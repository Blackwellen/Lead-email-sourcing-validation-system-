"""Bounded, polite, single-domain crawler.

Never crawls a whole site: homepage -> contact -> imprint -> team -> locations -> about ->
keyword service pages -> sitemap-discovered contact pages -> a couple of linked PDFs, under hard
caps for pages, depth, PDFs and wall-clock time. Only same-registrable-domain links are followed,
robots.txt is honoured, everything goes through the shared rate limiter, and every failure class
is recorded on the result instead of raised.
"""
from __future__ import annotations

import asyncio
import functools
import heapq
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import safe_http as sh
from .extract import (
    extract_from_html,
    extract_from_pdf_bytes,
    extract_from_vcard,
    rank_internal_links,
)
from .ratelimit import RateLimiter, WaitTooLong
from .robots import Decision, RobotsCache
from .sitemap import parse_sitemap, pick_contact_urls
from .types import EmailObservation, OrgMetadata, PageExtraction, SiteEvidence
from .urls import host_of, normalise_url, registrable_domain, site_key, to_ascii_host

log = logging.getLogger(__name__)

JsRenderer = Callable[[str], Awaitable[str | None]]
PageCallback = Callable[["PageRecord", PageExtraction | None], Awaitable[None] | None]


@dataclass(slots=True)
class CrawlOptions:
    max_pages: int = 12
    max_depth: int = 2
    max_pdfs: int = 2
    time_budget_s: float = 60.0
    keywords: tuple[str, ...] = ()
    use_sitemap: bool = True
    max_sitemap_fetches: int = 2
    max_sitemap_picks: int = 6
    allow_js_render: bool = False
    js_renderer: JsRenderer | None = None
    follow_cross_domain_redirect: bool = False
    max_page_bytes: int | None = None
    max_pdf_bytes: int = 5_000_000
    company_hint: str | None = None
    respect_robots: bool = True
    max_rate_wait_s: float = 20.0
    guess_standard_paths: bool = True
    link_limit: int = 30


@dataclass(slots=True)
class PageRecord:
    url: str
    final_url: str
    depth: int
    kind: str  # page | pdf | vcard | sitemap | robots
    reason: str = ""
    status: int | None = None
    content_type: str | None = None
    bytes: int = 0
    error_code: str | None = None
    error_detail: str | None = None
    not_modified: bool = False
    from_js: bool = False
    truncated: bool = False
    n_emails: int = 0
    elapsed_ms: int = 0
    title: str | None = None
    content_hash: str | None = None


@dataclass(slots=True)
class DomainHealth:
    reachable: bool = False
    dns_failure: bool = False
    tls_failure: bool = False
    timeout: bool = False
    blocked_ssrf: bool = False
    robots_deferred: bool = False
    robots_blocked_homepage: bool = False
    rate_limited: bool = False
    parked: str | None = None
    redirected_domain: str | None = None  # registrable domain of the final URL when it differs
    final_url: str | None = None
    status: int | None = None


@dataclass(slots=True)
class CrawlStats:
    pages_attempted: int = 0
    pages_ok: int = 0
    pages_failed: int = 0
    not_modified: int = 0
    robots_blocked: int = 0
    pdfs: int = 0
    sitemap_urls: int = 0
    js_rendered: int = 0
    bytes: int = 0
    observations: int = 0
    unique_emails: int = 0
    rate_limited: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0


@dataclass(slots=True)
class AggregatedEmail:
    """One distinct address with ALL pages that published it."""

    email: str  # canonical lower-case
    observations: list[EmailObservation] = field(default_factory=list)
    page_urls: list[str] = field(default_factory=list)
    methods: set[str] = field(default_factory=set)

    @property
    def name(self) -> str | None:
        return next((o.name for o in self.observations if o.name), None)


@dataclass(slots=True)
class CrawlResult:
    domain: str
    start_url: str | None
    pages: list[PageRecord] = field(default_factory=list)
    observations: list[EmailObservation] = field(default_factory=list)
    emails: list[AggregatedEmail] = field(default_factory=list)
    org_metadata: list[OrgMetadata] = field(default_factory=list)
    health: DomainHealth = field(default_factory=DomainHealth)
    stats: CrawlStats = field(default_factory=CrawlStats)
    stopped_reason: str = "completed"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    _extractions: list[PageExtraction] = field(default_factory=list, repr=False)

    def site_evidence(self, source_kind: str = "guess") -> SiteEvidence:
        ev = SiteEvidence(domain=self.domain, final_url=self.health.final_url, reachable=self.health.reachable,
                          status=self.health.status, parked=self.health.parked,
                          redirected_domain=self.health.redirected_domain, dns_failure=self.health.dns_failure,
                          tls_failure=self.health.tls_failure, observed_at=self.finished_at or datetime.now(UTC),
                          pages=self.stats.pages_ok, source_kind=source_kind)
        for x in self._extractions:
            if x.title:
                ev.titles.append(x.title)
            ev.h1s.extend(x.h1[:3])
            if x.site_name:
                ev.site_names.append(x.site_name)
            ev.org.extend(x.org)
            if x.footer_text:
                ev.footer_texts.append(x.footer_text)
            if x.text_sample:
                ev.text_samples.append(x.text_sample)
            for src, dst in ((x.company_numbers, ev.company_numbers), (x.postcodes, ev.postcodes), (x.phones, ev.phones)):
                for v in src:
                    if v not in dst:
                        dst.append(v)
        return ev


# --------------------------------------------------------------------------- parked-domain detection
_PARKING_HOSTS = {
    "sedoparking.com", "sedo.com", "parkingcrew.net", "bodis.com", "hugedomains.com", "dan.com", "afternic.com",
    "above.com", "parked.com", "domainsponsor.com", "fabulous.com", "uniregistry.com", "undeveloped.com",
    "godaddy.com", "namecheap.com", "123-reg.co.uk", "register.com", "ztomy.com", "voodoo.com",
}
_PARK_RULES: list[tuple[str, re.Pattern[str], int]] = [
    ("domain_for_sale", re.compile(
        r"this domain (?:name )?(?:is|may be|might be) (?:currently )?(?:for sale|available)|buy this domain|"
        r"domain (?:name )?(?:is )?for sale|make an offer (?:on|for) this domain|inquire about this domain|"
        r"purchase this domain|is for sale!|hugedomains|afternic|sedo\.com|dan\.com/buy|get this domain|"
        r"this web ?site is for sale|interested in this domain|the domain .{1,60} is for sale", re.I), 12000),
    ("parked", re.compile(
        r"domain (?:is )?(?:currently )?parked|parked (?:free|domain|page|courtesy)|parked by|parking page|sedoparking|"
        r"parkingcrew|this domain is registered (?:at|with|through)|domain registered at|registrar parking|"
        r"related searches|sponsored listings", re.I), 4000),
    ("expired", re.compile(
        r"this domain (?:name )?has expired|domain (?:name )?(?:has )?expired|renew (?:your|this) domain|"
        r"domain expir(?:ed|ation)|expired domain|domain will be (?:deleted|released)", re.I), 6000),
    ("default_page", re.compile(
        r"welcome to nginx|apache2 (?:ubuntu|debian|centos)? ?default page|test page for the apache|"
        r"future home of something quick|default web site page|iis windows server|plesk default page|"
        r"web server's default page|default server page|this is the default web page|congratulations.{0,40}web server",
        re.I), 2500),
    ("suspended", re.compile(
        r"account (?:has been )?suspended|this (?:web)?site (?:has been|is) suspended|bandwidth limit exceeded|"
        r"website is not available|site (?:is )?temporarily unavailable|domain (?:has been )?suspended", re.I), 2500),
]


def detect_parked(x: PageExtraction | None, final_url: str | None) -> str | None:
    """Fingerprint parked / for-sale / expired / default-server / suspended pages."""
    if final_url:
        reg = registrable_domain(final_url)
        if reg in _PARKING_HOSTS:
            return "parking_host"
    if x is None:
        return None
    hay = f"{x.title or ''} {' '.join(x.h1)} {x.text_sample}".lower()
    for reason, rx, max_len in _PARK_RULES:
        if x.text_length <= max_len and rx.search(hay):
            return reason
    return None


# --------------------------------------------------------------------------- JS rendering (optional)
_JS_POOL: asyncio.Semaphore | None = None
_JS_BROWSER = None
_JS_LOCK: asyncio.Lock | None = None


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


async def render_with_playwright(url: str, *, guard: sh.Guard | None = None, timeout_s: float = 20.0,
                                 pool_size: int = 2) -> str | None:
    """Render a page with headless Chromium and return the DOM HTML, or None if unavailable.

    Safety: only used when explicitly enabled and robots.txt already allowed the URL. Every
    request the browser makes is re-checked against the SSRF guard (scheme/port/resolved IPs) and
    non-document sub-resources (images, media, fonts) are aborted. Downloads are not accepted.
    Limitation: the browser performs its own DNS resolution, so unlike ``SafeFetcher`` this path
    cannot pin the connection to the validated address.
    """
    global _JS_POOL, _JS_BROWSER, _JS_LOCK
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    guard = guard or sh.Guard()
    try:
        target = guard.check_url(url)
        await guard.resolve(target.host, target.port)
    except Exception:
        return None
    if _JS_POOL is None:
        _JS_POOL = asyncio.Semaphore(pool_size)
        _JS_LOCK = asyncio.Lock()
    async with _JS_POOL:
        assert _JS_LOCK is not None
        try:
            async with _JS_LOCK:
                if _JS_BROWSER is None or not _JS_BROWSER.is_connected():
                    pw = await async_playwright().start()
                    _JS_BROWSER = await pw.chromium.launch(headless=True)
            ctx = await _JS_BROWSER.new_context(accept_downloads=False, java_script_enabled=True,
                                                user_agent=None, ignore_https_errors=False)
            try:
                page = await ctx.new_page()
                checked: dict[str, bool] = {}

                async def gate(route):
                    req = route.request
                    if req.resource_type in ("image", "media", "font", "stylesheet"):
                        return await route.abort()
                    try:
                        t = guard.check_url(req.url)
                        key = f"{t.host}:{t.port}"
                        if key not in checked:
                            await guard.resolve(t.host, t.port)
                            checked[key] = True
                    except Exception:
                        return await route.abort()
                    await route.continue_()

                await page.route("**/*", gate)
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                try:
                    await page.wait_for_load_state("networkidle", timeout=int(timeout_s * 500))
                except Exception:
                    pass
                html = await page.content()
                return html[:3_000_000]
            finally:
                await ctx.close()
        except Exception as e:
            log.debug("playwright render failed for %s: %s", url, e)
            return None


# --------------------------------------------------------------------------- crawler
_STD_PATHS = ("/contact", "/contact-us", "/about")


def _normalise_input(domain_or_url: str) -> tuple[list[str], str, bool]:
    """-> (start URL candidates, bare host key, had_scheme)"""
    v = domain_or_url.strip()
    had_scheme = "://" in v
    if not had_scheme:
        v = "https://" + v.lstrip("/")
    p = urlsplit(v)
    host = to_ascii_host(p.hostname or "") if p.hostname else None
    if not host:
        return [], "", had_scheme
    netloc = host + (f":{p.port}" if p.port else "")
    path = p.path or "/"
    first = normalise_url(f"{p.scheme}://{netloc}{path}" + (f"?{p.query}" if p.query else ""))
    cands = [first] if first else []
    if not had_scheme and first:
        cands.append(first.replace("https://", "http://", 1))
    return cands, host, had_scheme


class _Crawler:
    def __init__(self, fetcher: sh.SafeFetcher, domain: str, options: CrawlOptions,
                 on_page: PageCallback | None, limiter: RateLimiter, robots: RobotsCache, group: str | None) -> None:
        self.f, self.o, self.on_page, self.limiter, self.robots, self.group = fetcher, options, on_page, limiter, robots, group
        self.res = CrawlResult(domain=domain, start_url=None)
        self.deadline = time.monotonic() + options.time_budget_s
        self.allowed_keys: set[str] = set()
        self.seen: set[str] = set()
        self.heap: list[tuple[int, int, int, str, str]] = []
        self.pdf_candidates: dict[str, tuple[int, str]] = {}
        self.seq = 0
        self.stop = False
        self._last_extraction: PageExtraction | None = None
        self._last_result: sh.FetchResult | None = None

    # -- helpers
    def same_site(self, url: str) -> bool:
        k = site_key(url)
        return k is not None and k in self.allowed_keys

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def push(self, url: str, score: int, depth: int, reason: str) -> None:
        n = normalise_url(url)
        if not n or n in self.seen or not self.same_site(n):
            return
        self.seen.add(n)
        self.seq += 1
        heapq.heappush(self.heap, (-score, self.seq, depth, n, reason))

    def _count_error(self, code: str) -> None:
        self.res.stats.errors[code] = self.res.stats.errors.get(code, 0) + 1

    async def _emit(self, rec: PageRecord, x: PageExtraction | None) -> None:
        if self.on_page is None:
            return
        try:
            r = self.on_page(rec, x)
            if inspect.isawaitable(r):
                await r
        except Exception as e:
            log.warning("on_page callback failed: %s", e)

    # -- fetching with robots + limiter
    async def fetch(self, url: str, *, kind: str, depth: int, reason: str, accept=None, max_bytes: int | None = None,
                    conditional: bool = True) -> tuple[PageRecord, sh.FetchResult | None]:
        rec = PageRecord(url=url, final_url=url, depth=depth, kind=kind, reason=reason)
        host = host_of(url) or url
        if self.o.respect_robots:
            chk = await self.robots.check(url)
            if chk.decision is Decision.DEFERRED:
                if chk.error_code:  # transport failure (dns/tls/timeout/ssrf ...): report it as such
                    rec.error_code, rec.error_detail = chk.error_code, chk.reason
                    self._count_error(chk.error_code)
                    self.res.stats.pages_failed += 1
                    return rec, None
                self.res.health.robots_deferred = True
                rec.error_code, rec.error_detail = "robots_deferred", chk.reason
                self._count_error("robots_deferred")
                return rec, None
            if chk.decision is Decision.DISALLOWED:
                rec.error_code, rec.error_detail = "robots_disallowed", chk.reason
                self.res.stats.robots_blocked += 1
                self._count_error("robots_disallowed")
                return rec, None
            if chk.crawl_delay:
                self.limiter.set_crawl_delay(host, chk.crawl_delay)
        result: sh.FetchResult | None = None
        for attempt in range(2):
            budget = self.remaining()
            if budget <= 0:
                rec.error_code = "time_budget"
                self.stop = True
                self.res.stopped_reason = "time_budget"
                return rec, None
            try:
                async with self.limiter.acquire(host, group=self.group, max_wait=min(budget, self.o.max_rate_wait_s)) as slot:
                    result = await self.f.fetch(url, accept=accept, max_bytes=max_bytes, conditional=conditional)
                    slot.report(result.status, result.retry_after() if result.status in (429, 503) else None)
            except WaitTooLong as e:
                rec.error_code, rec.error_detail = "rate_limited", str(e)
                self.res.health.rate_limited = True
                self.res.stats.rate_limited += 1
                self._count_error("rate_limited")
                if self.limiter.backoff_remaining(host) > 0:
                    self.stop = True
                    self.res.stopped_reason = "rate_limited"
                return rec, None
            self.res.stats.pages_attempted += 1
            if result.status in (429, 503) and attempt == 0:
                self.res.stats.rate_limited += 1
                continue  # one polite retry after the limiter's backoff
            break
        assert result is not None
        rec.final_url, rec.status, rec.content_type = result.final_url, result.status, result.content_type
        rec.bytes, rec.truncated = len(result.body), result.truncated
        rec.not_modified, rec.content_hash = result.not_modified, result.content_hash
        rec.elapsed_ms = result.elapsed_ms
        if result.error_code:
            rec.error_code, rec.error_detail = result.error_code, result.error_detail
        elif result.status is not None and result.status >= 400:
            rec.error_code = f"http_{result.status}"
        self.res.stats.bytes += len(result.body)
        if result.status in (429, 503):
            self.res.health.rate_limited = True
            self.res.stats.rate_limited += 1
            self.stop = True
            self.res.stopped_reason = "rate_limited"
        return rec, result

    async def _extract(self, kind: str, result: sh.FetchResult, url: str) -> PageExtraction | None:
        body = result.body
        ctype = result.content_type or ""
        big = len(body) > 150_000
        if kind == "pdf":
            return await asyncio.to_thread(extract_from_pdf_bytes, body, result.final_url)
        text = result.text()
        if kind == "vcard":
            ex = PageExtraction(page_url=result.final_url)
            ex.emails = extract_from_vcard(text, result.final_url)
            return ex
        if "html" in ctype or "xml" in ctype or ctype == "text/plain" or not ctype:
            if big:
                return await asyncio.to_thread(extract_from_html, text, result.final_url, company_hint=self.o.company_hint)
            return extract_from_html(text, result.final_url, company_hint=self.o.company_hint)
        return None

    def _absorb(self, rec: PageRecord, x: PageExtraction | None) -> None:
        if x is None:
            return
        rec.title = x.title
        rec.n_emails = len(x.emails)
        self.res.observations.extend(x.emails)
        self.res._extractions.append(x)
        self.res.stats.observations += len(x.emails)

    def _queue_links(self, x: PageExtraction, base: str, depth: int) -> None:
        if x.nofollow:
            return
        ranked = rank_internal_links(x.links, base, keywords=self.o.keywords, is_same_site=self.same_site,
                                     limit=self.o.link_limit)
        for r in ranked:
            if r.url.lower().split("?")[0].endswith(".pdf"):
                if r.url not in self.seen:
                    cur = self.pdf_candidates.get(r.url)
                    if cur is None or r.score > cur[0]:
                        self.pdf_candidates[r.url] = (r.score, r.reason)
                continue
            if depth + 1 <= self.o.max_depth:
                self.push(r.url, r.score, depth + 1, r.reason)

    async def process(self, url: str, depth: int, reason: str, kind: str = "page") -> PageRecord:
        accept = None
        max_bytes = self.o.max_page_bytes
        if kind == "pdf":
            accept = {"application/pdf", "application/octet-stream"}
            max_bytes = self.o.max_pdf_bytes
        elif url.lower().split("?")[0].endswith(".vcf"):
            kind = "vcard"
            accept = {"text/vcard", "text/x-vcard", "text/plain", "text/directory"}
        # The homepage is always fetched in full: a 304 carries no body, hence no links to follow.
        rec, result = await self.fetch(url, kind=kind, depth=depth, reason=reason, accept=accept, max_bytes=max_bytes,
                                       conditional=reason != "homepage")
        x: PageExtraction | None = None
        if result is not None:
            if result.not_modified:
                self.res.stats.not_modified += 1
                self.res.stats.pages_ok += 1
            elif result.usable_body and result.status is not None and 200 <= result.status < 300:
                try:
                    x = await self._extract(kind, result, url)
                except Exception as e:  # extraction must never break the crawl
                    rec.error_code, rec.error_detail = "extract_error", str(e)[:200]
                self.res.stats.pages_ok += 1
            else:
                self.res.stats.pages_failed += 1
            if rec.error_code and rec.error_code != "extract_error":
                self._count_error(rec.error_code)
        self._absorb(rec, x)
        self.res.pages.append(rec)
        await self._emit(rec, x)
        self._last_extraction = x
        self._last_result = result
        return rec


async def crawl_domain(
    fetcher: sh.SafeFetcher,
    domain_or_url: str,
    *,
    options: CrawlOptions | None = None,
    on_page: PageCallback | None = None,
    limiter: RateLimiter | None = None,
    robots: RobotsCache | None = None,
    group: str | None = None,
) -> CrawlResult:
    """Crawl one domain within the caps in ``options``. Never raises (except cancellation)."""
    o = options or CrawlOptions()
    t0 = time.monotonic()
    cands, host, had_scheme = _normalise_input(domain_or_url)
    cr = _Crawler(fetcher, host or domain_or_url, o, on_page, limiter or RateLimiter(min_interval=1.0),
                  robots or RobotsCache(fetcher), group)
    res = cr.res
    try:
        if not cands:
            res.stopped_reason = "invalid_input"
            res.health.blocked_ssrf = False
            res.stats.errors["invalid_url"] = 1
            return res
        await _run(cr, cands, host, had_scheme)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("crawl of %s failed unexpectedly", domain_or_url)
        res.stopped_reason = f"internal_error: {type(e).__name__}"
    finally:
        _finalise(cr, t0)
    return res


def _finalise(cr: _Crawler, t0: float) -> None:
    res = cr.res
    agg: dict[str, AggregatedEmail] = {}
    for ob in res.observations:
        k = ob.email.lower()
        a = agg.get(k)
        if a is None:
            a = agg[k] = AggregatedEmail(email=k)
        a.observations.append(ob)
        if ob.page_url not in a.page_urls:
            a.page_urls.append(ob.page_url)
        a.methods.add(ob.method)
        a.methods.update(ob.also_methods)
    res.emails = list(agg.values())
    res.stats.unique_emails = len(agg)
    seen_org: set[tuple[str, str]] = set()
    for x in res._extractions:
        for om in x.org:
            key = ((om.name or om.legal_name or "").lower(), (om.postcode or "").lower())
            if key not in seen_org:
                seen_org.add(key)
                res.org_metadata.append(om)
    res.stats.elapsed_s = round(time.monotonic() - t0, 3)
    res.finished_at = datetime.now(UTC)


async def _run(cr: _Crawler, cands: list[str], host: str, had_scheme: bool) -> None:
    res, o, h = cr.res, cr.o, cr.res.health
    start_key = site_key(cands[0]) or host
    cr.allowed_keys.add(start_key)
    # ---- homepage (with documented scheme / www fallbacks for a bare domain)
    attempts = list(cands)
    if not had_scheme and not host.startswith("www.") and registrable_domain(host) == host:
        attempts.append(f"https://www.{host}/")
    home_rec = None
    home_x = None
    tried = dns_failures = 0
    for u in attempts:
        if cr.remaining() <= 0:
            break
        cr.seen.add(normalise_url(u) or u)
        rec = await cr.process(u, 0, "homepage")
        home_rec, home_x = rec, cr._last_extraction
        tried += 1
        code = rec.error_code
        dns_failures += code == "dns_failure"
        h.tls_failure = h.tls_failure or code == "tls_failure"
        h.timeout = h.timeout or code == "timeout"
        h.blocked_ssrf = h.blocked_ssrf or code == "blocked_ssrf"
        h.robots_blocked_homepage = h.robots_blocked_homepage or code == "robots_disallowed"
        if code not in ("dns_failure", "tls_failure", "connection_error", "timeout") or cr.stop:
            break  # success, an HTTP-level answer, or a policy block: do not try other variants
    h.dns_failure = bool(tried) and dns_failures == tried
    if home_rec is None:
        res.stopped_reason = "time_budget"
        return
    res.start_url = home_rec.url
    h.final_url, h.status = home_rec.final_url, home_rec.status
    h.reachable = bool(home_rec.status and home_rec.status < 400 and home_rec.error_code in (None, "too_large"))
    if not h.reachable:
        res.stopped_reason = f"homepage_unreachable:{home_rec.error_code or home_rec.status}"
        return
    final_key = site_key(home_rec.final_url) or start_key
    if final_key != start_key:
        h.redirected_domain = registrable_domain(home_rec.final_url) or final_key
        if not o.follow_cross_domain_redirect:
            h.parked = detect_parked(home_x, home_rec.final_url)
            res.stopped_reason = "redirected_to_other_domain"
            return
        cr.allowed_keys.add(final_key)
    # ---- JS fallback for script-only shells
    if home_x is not None and o.allow_js_render and not home_x.emails and home_x.script_count >= 1 and home_x.text_length < 400:
        renderer = o.js_renderer
        if renderer is None and playwright_available():
            renderer = functools.partial(render_with_playwright, guard=cr.f.guard)
        if renderer is not None:
            chk = await cr.robots.check(home_rec.final_url) if o.respect_robots else None
            if chk is None or chk.allowed:
                try:
                    html = await asyncio.wait_for(renderer(home_rec.final_url), timeout=max(1.0, min(30.0, cr.remaining())))
                except Exception:
                    html = None
                if html:
                    jx = await asyncio.to_thread(extract_from_html, html, home_rec.final_url, company_hint=o.company_hint)
                    res.stats.js_rendered += 1
                    home_rec.from_js = True
                    cr._absorb(home_rec, jx)
                    home_x = jx
    h.parked = detect_parked(home_x, home_rec.final_url)
    if h.parked:
        res.stopped_reason = f"parked:{h.parked}"
        return
    if home_x is not None:
        cr._queue_links(home_x, home_rec.final_url, 0)
    origin = f"{urlsplit(home_rec.final_url).scheme}://{urlsplit(home_rec.final_url).netloc}"
    if o.guess_standard_paths and not any(r == "contact" for *_, r in cr.heap):
        for i, p in enumerate(_STD_PATHS[:2]):
            cr.push(origin + p, 30 - i, 1, "guess")
    # ---- sitemap
    if o.use_sitemap and not cr.stop:
        await _sitemap_discovery(cr, origin, home_rec.final_url)
    # ---- main frontier
    pages_done = 1
    while cr.heap and pages_done < o.max_pages and not cr.stop:
        if cr.remaining() <= 0:
            res.stopped_reason = "time_budget"
            break
        _neg, _seq, depth, url, reason = heapq.heappop(cr.heap)
        rec = await cr.process(url, depth, reason)
        if rec.error_code not in ("robots_disallowed", "robots_deferred"):
            pages_done += 1
        x = cr._last_extraction
        if x is not None and rec.kind == "page":
            cr._queue_links(x, rec.final_url, depth)
    if pages_done >= o.max_pages and cr.heap and res.stopped_reason == "completed":
        res.stopped_reason = "max_pages"
    # ---- PDFs last (small cap)
    if o.max_pdfs > 0 and not cr.stop and cr.remaining() > 0:
        picks = sorted(cr.pdf_candidates.items(), key=lambda kv: (-kv[1][0], kv[0]))[: o.max_pdfs]
        for url, (_score, reason) in picks:
            if cr.stop or cr.remaining() <= 0:
                break
            n = normalise_url(url)
            if n:
                cr.seen.add(n)
            rec = await cr.process(url, 1, reason or "pdf", kind="pdf")
            if rec.error_code is None:
                res.stats.pdfs += 1


async def _sitemap_discovery(cr: _Crawler, origin: str, base_url: str) -> None:
    o = cr.o
    try:
        urls = await cr.robots.sitemaps(origin + "/") if o.respect_robots else []
    except Exception:
        urls = []
    queue = [u for u in urls if cr.same_site(u)] or [origin + "/sitemap.xml"]
    fetched = 0
    picked: list = []
    while queue and fetched < o.max_sitemap_fetches and cr.remaining() > 0 and not cr.stop:
        smu = queue.pop(0)
        rec, result = await cr.fetch(smu, kind="sitemap", depth=1, reason="sitemap", accept=sh.SITEMAP_TYPES,
                                     max_bytes=5_000_000)
        fetched += 1
        cr.res.pages.append(rec)
        if result is None or not result.ok or not result.body:
            continue
        sm = parse_sitemap(result.body)
        cr.res.stats.sitemap_urls += len(sm.urls)
        for child in sm.sitemaps[:3]:
            if cr.same_site(child) and child not in queue:
                queue.append(child)
        if sm.urls:
            picked.extend(pick_contact_urls(sm.urls, base_url, keywords=o.keywords, is_same_site=cr.same_site,
                                            limit=o.max_sitemap_picks))
    picked.sort(key=lambda r: (-r.score, len(r.url)))
    for r in picked[: o.max_sitemap_picks]:
        if r.url.lower().endswith(".pdf"):
            cur = cr.pdf_candidates.get(r.url)
            if cur is None or r.score > cur[0]:
                cr.pdf_candidates[r.url] = (r.score, r.reason)
        else:
            cr.push(r.url, r.score, 1, "sitemap:" + r.reason)
