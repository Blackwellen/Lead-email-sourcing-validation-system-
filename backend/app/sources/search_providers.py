"""Optional search-based URL discovery.

Providers (all DISABLED until configured; no Google/Bing scraping, no CAPTCHA or quota workarounds):
  * ManualSearchResultsImport -- operator-supplied CSV of ``query,url,title[,snippet,rank]``
  * SearxngProvider           -- a metasearch instance the operator owns / is authorised to use (JSON API)
  * BraveSearchProvider       -- official Brave Search API with the operator's API key; stops on 429/quota
  * CommonCrawlIndexProvider  -- index.commoncrawl.org CDX lookups: URL discovery for known domains, NOT business discovery

Results are only *candidate* official websites.  :func:`resolve_official_websites` drops directories, social
networks, marketplaces and other aggregators; deciding which candidate is the *confirmed* domain of a business is
the identity engine's job.
"""
from __future__ import annotations

import csv
import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ..config import get_settings
from .base import (
    AdapterError,
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    RatePolicy,
    RetryPolicy,
    SourceAdapter,
    register,
)
from .persist import normalise_name
from .urls import NormalisedSite, normalise_website


class QuotaExhausted(AdapterError):
    """Provider quota used up / HTTP 429: the run must stop; there is deliberately no workaround."""


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0
    provider: str = ""
    query: str = ""


# --------------------------------------------------------------------------- aggregator blocklist
# Registrable domains (or bare labels with a public suffix wildcard handled by `_is_blocked`).
AGGREGATOR_DOMAINS: frozenset[str] = frozenset("""
yell.com thomsonlocal.com freeindex.co.uk checkatrade.com trustatrader.com ratedpeople.com mybuilder.com bark.com
cylex-uk.co.uk hotfrog.co.uk scoot.co.uk misterwhat.co.uk tuugo.co.uk 192.com bt.com yelp.com yelp.co.uk
trustpilot.com reviews.io feefo.com google.com google.co.uk goo.gl bing.com yahoo.com duckduckgo.com baidu.com
facebook.com instagram.com twitter.com x.com linkedin.com youtube.com tiktok.com pinterest.com snapchat.com
reddit.com quora.com medium.com tumblr.com threads.net wa.me whatsapp.com telegram.org
amazon.com amazon.co.uk ebay.com ebay.co.uk etsy.com gumtree.com alibaba.com aliexpress.com onbuy.com
fiverr.com upwork.com peopleperhour.com freelancer.com guru.com
rightmove.co.uk zoopla.co.uk onthemarket.com primelocation.com propertymark.co.uk nethouseprices.com
tripadvisor.com tripadvisor.co.uk booking.com expedia.com expedia.co.uk airbnb.com airbnb.co.uk hotels.com
trivago.com kayak.com lastminute.com opentable.com viator.com
companieshouse.gov.uk company-information.service.gov.uk opencorporates.com endole.co.uk dnb.com zoominfo.com
crunchbase.com companycheck.co.uk company-director-check.co.uk
wikipedia.org wikidata.org wikimedia.org wikihow.com archive.org
gov.uk nhs.uk
europages.com kompass.com uk.kompass.com
eventbrite.com eventbrite.co.uk meetup.com
wix.com
""".split())

# Shared hosting platforms: many unrelated businesses share one registrable domain, so de-duplicate by host.
MULTI_TENANT_DOMAINS: frozenset[str] = frozenset({
    "wixsite.com", "wordpress.com", "blogspot.com", "weebly.com", "squarespace.com", "godaddysites.com",
    "business.site", "myshopify.com", "webflow.io", "netlify.app", "github.io", "vercel.app",
})

_SOCIAL_HINT = re.compile(r"(?:^|\.)(?:facebook|instagram|linkedin|twitter|tiktok|youtube|pinterest)\.[a-z.]+$")


def _is_blocked(site: NormalisedSite) -> str | None:
    reg, host = site.registrable, site.host
    if reg in AGGREGATOR_DOMAINS or host in AGGREGATOR_DOMAINS:
        return "aggregator_or_directory"
    if any(host.endswith("." + d) for d in AGGREGATOR_DOMAINS if d.count(".") == 1):
        return "aggregator_subdomain"
    if _SOCIAL_HINT.search(host):
        return "social_network"
    # *.gov.uk / nhs.uk / ac.uk are institutions, not commercial targets here
    if reg.endswith((".gov.uk", ".nhs.uk", ".ac.uk", ".sch.uk", ".police.uk", ".mod.uk")):
        return "public_sector"
    return None


@dataclass
class WebsiteCandidate:
    url: str
    host: str
    registrable: str
    best_rank: int
    title: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    hits: int = 1
    status: str = "candidate"  # never 'confirmed' -- identity engine decides


def resolve_official_websites(
    hits: list[SearchHit], *, business_name: str | None = None, locality: str | None = None, max_candidates: int = 5
) -> tuple[list[WebsiteCandidate], list[dict[str, str]]]:
    """Reduce raw hits to candidate official-website roots.  Returns ``(candidates, dropped)``.

    One candidate per registrable domain (the root URL), ordered by a transparent heuristic score:
    rank, and whether business-name tokens appear in the host/title.  Nothing here is "confirmed".
    """
    cands: dict[str, WebsiteCandidate] = {}
    dropped: list[dict[str, str]] = []
    name_toks = set(normalise_name(business_name).split()) if business_name else set()
    for h in hits:
        site = normalise_website(h.url)
        if site is None:
            dropped.append({"url": h.url, "reason": "invalid_url"})
            continue
        why = _is_blocked(site)
        if why:
            dropped.append({"url": h.url, "reason": why})
            continue
        key = site.host if site.registrable in MULTI_TENANT_DOMAINS else site.registrable
        c = cands.get(key)
        if c is None:
            c = cands[key] = WebsiteCandidate(
                url=f"{site.url.split('://')[0]}://{site.host}", host=site.host, registrable=site.registrable,
                best_rank=h.rank or 999, title=h.title,
            )
        else:
            c.hits += 1
            if (h.rank or 999) < c.best_rank:
                c.best_rank, c.title = h.rank or 999, h.title or c.title
    for c in cands.values():
        score = max(0.0, 1.0 - 0.1 * (c.best_rank - 1)) * 0.5 + min(c.hits, 3) * 0.05
        c.reasons.append(f"rank={c.best_rank}")
        if name_toks:
            hay = set(re.findall(r"[a-z0-9]+", (c.registrable + " " + c.title).lower()))
            overlap = len(name_toks & hay) / len(name_toks)
            score += 0.4 * overlap
            if overlap:
                c.reasons.append(f"name_overlap={overlap:.2f}")
        c.score = round(min(score, 1.0), 3)
    ordered = sorted(cands.values(), key=lambda c: (-c.score, c.best_rank, c.registrable))
    return ordered[:max_candidates], dropped


# --------------------------------------------------------------------------- providers
class SearchProvider(ABC):
    provider_id: ClassVar[str]

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self.stop_reason: str | None = None

    @abstractmethod
    async def search(self, query: str, *, country: str | None = None, count: int = 10, offset: int = 0) -> list[SearchHit]: ...

    @abstractmethod
    async def health(self) -> AdapterHealth: ...

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        c = self.config.get("http_client")
        if c is not None:
            return c, False
        s = get_settings()
        ua = s.user_agent.replace("set LEAD_CONTACT_EMAIL", s.contact_email or "unset")
        from ..crawler.safe_http import safe_client

        # SSRF-safe: base_url (SearXNG) is operator-supplied; every hop validated, connections pinned to public IPs
        return safe_client(timeout=float(self.config.get("timeout_s", 20.0)), headers={"User-Agent": ua}, follow_redirects=False), True


class ManualSearchResultsImport(SearchProvider):
    """Operator-curated CSV of search results (query,url,title[,snippet,rank]).  No network."""

    provider_id = "manual_search_results"
    MAX_ROWS = 200_000

    def _rows(self):
        p = self.config.get("path")
        if not p:
            raise NotConfigured("manual_search_results needs config 'path'")
        path = Path(p)
        if path.stat().st_size > int(self.config.get("max_bytes", 20 * 1024 * 1024)):
            raise AdapterError("search results CSV too large")
        csv.field_size_limit(1 << 20)
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
            rd = csv.DictReader(fh)
            for i, row in enumerate(rd):
                if i >= self.MAX_ROWS:
                    raise AdapterError("too many rows")
                yield {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

    async def search(self, query: str, *, country: str | None = None, count: int = 10, offset: int = 0) -> list[SearchHit]:
        q = normalise_name(query)
        out: list[SearchHit] = []
        for r in self._rows():
            if normalise_name(r.get("query", "")) != q:
                continue
            if r.get("url", "").startswith(("=", "@")):
                continue
            try:
                rank = int(r.get("rank") or len(out) + 1)
            except ValueError:
                rank = len(out) + 1
            out.append(SearchHit(r.get("url", ""), r.get("title", "")[:300], r.get("snippet", "")[:500], rank, self.provider_id, query))
        out.sort(key=lambda h: h.rank)
        return out[offset: offset + count]

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if not p:
            return AdapterHealth(False, "not_configured", "not configured: no CSV path")
        return AdapterHealth(Path(p).is_file(), "ok" if Path(p).is_file() else "unavailable", str(p))


class SearxngProvider(SearchProvider):
    """Self-hosted SearXNG.  Only valid against an instance the operator owns or is explicitly authorised to query."""

    provider_id = "searxng"

    def _base(self) -> str:
        base = (self.config.get("base_url") or "").rstrip("/")
        if not base:
            raise NotConfigured("not configured: set searxng 'base_url' (operator-owned instance)")
        if not self.config.get("authorised"):
            raise NotConfigured("not configured: set 'authorised': true to confirm the instance is operator-owned/authorised")
        return base

    async def search(self, query: str, *, country: str | None = None, count: int = 10, offset: int = 0) -> list[SearchHit]:
        base = self._base()
        page = offset // max(count, 1) + 1
        params = {"q": query, "format": "json", "pageno": page, "safesearch": 0}
        if country:
            params["language"] = f"en-{country.upper()}"
        client, own = self._client()
        try:
            r = await client.get(base + "/search", params=params)
        finally:
            if own:
                await client.aclose()
        if r.status_code == 429:
            self.stop_reason = "quota_exhausted"
            raise QuotaExhausted("SearXNG instance returned 429; stopping")
        if r.status_code == 403:
            raise AdapterError("SearXNG refused the JSON format (enable 'json' under search.formats in settings.yml)")
        if r.status_code >= 400:
            raise AdapterError(f"SearXNG HTTP {r.status_code}")
        data = r.json()
        res = data.get("results", [])[:count]
        return [SearchHit(x.get("url", ""), x.get("title", "") or "", x.get("content", "") or "", i + 1 + offset, self.provider_id, query)
                for i, x in enumerate(res) if isinstance(x, dict)]

    async def health(self) -> AdapterHealth:
        try:
            self._base()
        except NotConfigured as e:
            return AdapterHealth(False, "not_configured", str(e))
        return AdapterHealth(True, "ok", "configured (instance not probed)")


class BraveSearchProvider(SearchProvider):
    """Official Brave Search API.  Honours X-RateLimit-* headers; stops on HTTP 429 or an exhausted window."""

    provider_id = "brave_search"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.remaining: list[int] | None = None
        self.exhausted = False
        self.requests_made = 0

    def _key(self) -> str:
        k = self.config.get("api_key")
        if not k:
            raise NotConfigured("not configured: Brave Search API key missing")
        return str(k)

    @staticmethod
    def _ints(v: str | None) -> list[int] | None:
        if not v:
            return None
        try:
            return [int(x.strip()) for x in v.split(",")]
        except ValueError:
            return None

    async def search(self, query: str, *, country: str | None = None, count: int = 10, offset: int = 0) -> list[SearchHit]:
        key = self._key()
        if self.exhausted:
            raise QuotaExhausted("Brave quota exhausted earlier in this run")
        if self.requests_made >= int(self.config.get("max_requests", 100)):
            self.exhausted, self.stop_reason = True, "run_request_cap"
            raise QuotaExhausted("per-run request cap reached")
        params: dict[str, Any] = {"q": query, "count": min(count, 20), "offset": offset // max(min(count, 20), 1)}
        if country:
            params["country"] = country.upper()
        client, own = self._client()
        try:
            r = await client.get(self.ENDPOINT, params=params, headers={"Accept": "application/json", "X-Subscription-Token": key})
        finally:
            if own:
                await client.aclose()
        self.requests_made += 1
        self.remaining = self._ints(r.headers.get("x-ratelimit-remaining")) or self.remaining
        if r.status_code == 429:
            self.exhausted, self.stop_reason = True, "quota_exhausted"
            raise QuotaExhausted("Brave returned 429 (rate/quota exceeded); stopping -- no retry or workaround")
        if r.status_code in (401, 403):
            raise AdapterError(f"Brave API key rejected (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise AdapterError(f"Brave HTTP {r.status_code}")
        if self.remaining is not None and any(x <= 0 for x in self.remaining):
            self.exhausted, self.stop_reason = True, "quota_exhausted"  # finish parsing this response, then stop
        data = r.json()
        res = (data.get("web") or {}).get("results", [])
        return [SearchHit(x.get("url", ""), x.get("title", "") or "", x.get("description", "") or "", offset + i + 1, self.provider_id, query)
                for i, x in enumerate(res) if isinstance(x, dict)]

    async def health(self) -> AdapterHealth:
        try:
            self._key()
        except NotConfigured as e:
            return AdapterHealth(False, "not_configured", str(e))
        return AdapterHealth(not self.exhausted, "ok" if not self.exhausted else "degraded",
                             "key configured" if not self.exhausted else "quota exhausted",
                             {"remaining": self.remaining})


class CommonCrawlIndexProvider(SearchProvider):
    """Common Crawl CDX index lookups for a *known* domain/prefix (URL discovery, not business discovery)."""

    provider_id = "commoncrawl_index"
    BASE = "https://index.commoncrawl.org/"

    async def _latest_collection(self, client: httpx.AsyncClient) -> str:
        if self.config.get("collection"):
            return str(self.config["collection"])
        r = await client.get(self.BASE + "collinfo.json")
        if r.status_code >= 400:
            raise AdapterError(f"Common Crawl collinfo HTTP {r.status_code}")
        return r.json()[0]["id"]

    async def search(self, query: str, *, country: str | None = None, count: int = 10, offset: int = 0) -> list[SearchHit]:
        """``query`` is a domain or URL prefix, e.g. ``example.com`` or ``example.com/contact``."""
        q = query.strip()
        if not q or re.search(r"\s", q):
            raise AdapterError("Common Crawl lookups take a domain or URL prefix, not free text")
        client, own = self._client()
        try:
            coll = await self._latest_collection(client)
            params = {"url": q + ("" if "/" in q else "/*"), "output": "json", "limit": count, "filter": ["=status:200", "mime:text/html"],
                      "fl": "url,timestamp,status,mime"}
            r = await client.get(f"{self.BASE}{coll}-index", params=params)
        finally:
            if own:
                await client.aclose()
        if r.status_code in (429, 503):
            self.stop_reason = "quota_exhausted"
            raise QuotaExhausted(f"Common Crawl index asks us to slow down (HTTP {r.status_code}); stopping")
        if r.status_code == 404:
            return []  # "No Captures found"
        if r.status_code >= 400:
            raise AdapterError(f"Common Crawl HTTP {r.status_code}")
        hits = []
        for i, line in enumerate(r.text.splitlines()[:count]):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            hits.append(SearchHit(d.get("url", ""), "", f"crawled {d.get('timestamp', '')}", offset + i + 1, self.provider_id, query))
        return hits

    async def health(self) -> AdapterHealth:
        return AdapterHealth(True, "ok", "public index; not probed")


# --------------------------------------------------------------------------- adapters wrapping providers
class _SearchAdapter(SourceAdapter):
    provider_cls: ClassVar[type[SearchProvider]]

    def _provider(self) -> SearchProvider:
        return self.provider_cls(self.config)

    async def health(self) -> AdapterHealth:
        return await self._provider().health()

    def _queries(self, q: DiscoveryQuery) -> list[tuple[str, str | None]]:
        """(query string, locality) pairs: keywords x localities, capped (no combinatorial explosion)."""
        cap = int(self.config.get("max_queries", 20))
        kws = list(dict.fromkeys([*q.keywords, *q.synonyms]))[: int(self.config.get("max_keywords", 5))] or [""]
        places: list[str | None] = list(q.localities or q.regions) or [None]
        out = [(" ".join(x for x in (k, p) if x).strip(), p) for k in kws for p in places]
        return [x for x in out if x[0]][:cap]

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        prov = self._provider()
        cc = self.config.get("country_code") or (query.countries[0] if len(query.countries) == 1 else None)
        if not cc:
            raise NotConfigured("search discovery needs exactly one country (query or config 'country_code')")
        cp = checkpoint or {}
        qi0, hi0 = int(cp.get("qi", 0)), int(cp.get("hit", -1)) + 1
        queries = self._queries(query)
        observed = datetime.now(UTC)
        emitted = 0
        per_query = int(self.config.get("results_per_query", 10))
        for qi in range(qi0, len(queries)):
            qstr, place = queries[qi]
            try:
                hits = await prov.search(qstr, country=cc, count=per_query)
            except QuotaExhausted:
                self.stop_reason = prov.stop_reason
                return
            cands, _dropped = resolve_official_websites(hits, locality=place, max_candidates=per_query)
            for hi, c in enumerate(cands):
                if qi == qi0 and hi < hi0:
                    continue
                rec = BusinessRecord(
                    source_ref=c.host, trading_name=c.host, status="active", country_code=cc.upper(), city=place,
                    website=c.url, category_hint=qstr,
                    raw={"name_derived_from_domain": True, "title": c.title[:200], "query": qstr, "rank": c.best_rank,
                         "score": c.score, "candidate_status": "unconfirmed"},
                    source_url=None, licence=self.info.licence, observed_at=observed, adapter_id=self.info.adapter_id,
                )
                yield rec, {"qi": qi, "hit": hi}
                emitted += 1
                if query.limit and emitted >= query.limit:
                    return


@register
class ManualSearchResultsAdapter(_SearchAdapter):
    provider_cls = ManualSearchResultsImport
    info = AdapterInfo(
        adapter_id="manual_search_results", name="Imported search results (CSV)", country_coverage=("*",),
        source_type="manual_import", licence="operator-supplied", source_url="",
        access_requirements="Operator CSV with columns query,url,title[,snippet,rank]; config 'path'.",
        automated_collection_permitted=True, rate_policy=RatePolicy(None), fields_available=("website",),
        incremental_support=True, requires_config=True, live_verified=True,
        notes="Local file only. Candidate websites; identity engine confirms.",
    )


@register
class SearxngAdapter(_SearchAdapter):
    provider_cls = SearxngProvider
    info = AdapterInfo(
        adapter_id="searxng", name="SearXNG (self-hosted metasearch)", country_coverage=("*",),
        source_type="search_api", licence="Result licences vary; only URLs/titles are used as leads",
        source_url="https://docs.searxng.org/dev/search_api.html",
        access_requirements="Operator-owned or explicitly authorised instance; JSON format enabled; config base_url + authorised=true.",
        automated_collection_permitted=True, rate_policy=RatePolicy(30, 1), fields_available=("website",),
        incremental_support=True, requires_config=True, retry_policy=RetryPolicy(max_attempts=2),
        notes="Upstream engines' terms are the instance operator's responsibility.",
    )


@register
class BraveSearchAdapter(_SearchAdapter):
    provider_cls = BraveSearchProvider
    info = AdapterInfo(
        adapter_id="brave_search", name="Brave Search API", country_coverage=("*",),
        source_type="search_api", licence="Brave Search API terms (key holder's plan); results used as URL leads only",
        source_url="https://api.search.brave.com/app/documentation/web-search/get-started",
        access_requirements="API key from the operator's own Brave account (config api_key); free allowance applies.",
        automated_collection_permitted=True, rate_policy=RatePolicy(60, 1), fields_available=("website",),
        incremental_support=True, requires_config=True, retry_policy=RetryPolicy(max_attempts=1, stop_on_429=True),
        notes="Stops on 429 / exhausted X-RateLimit-Remaining; never retries around quotas. Check current plan terms for storage rights.",
    )


@register
class CommonCrawlAdapter(_SearchAdapter):
    provider_cls = CommonCrawlIndexProvider
    info = AdapterInfo(
        adapter_id="commoncrawl_index", name="Common Crawl URL index (URL discovery)", country_coverage=("*",),
        source_type="web_archive", licence="Common Crawl Terms of Use (index metadata); crawled content keeps its owners' rights",
        source_url="https://index.commoncrawl.org/",
        access_requirements="Public CDX API; polite use (about 1 request/second); provide a domain list.",
        automated_collection_permitted=True, rate_policy=RatePolicy(30, 1), fields_available=("website",),
        incremental_support=False, requires_config=True,
        notes="URL discovery for domains you already know, not a source of businesses.",
    )

    def _queries(self, q: DiscoveryQuery) -> list[tuple[str, str | None]]:
        doms = list(q.extra.get("domains") or self.config.get("domains") or [])
        return [(d, None) for d in doms[: int(self.config.get("max_queries", 50))]]


__all__ = [
    "SearchHit", "SearchProvider", "ManualSearchResultsImport", "SearxngProvider", "BraveSearchProvider",
    "CommonCrawlIndexProvider", "QuotaExhausted", "resolve_official_websites", "WebsiteCandidate", "AGGREGATOR_DOMAINS",
]
