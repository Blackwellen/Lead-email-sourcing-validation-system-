"""DISC-06: business-network expansion (partners / suppliers / members / brands / branches / franchises).

For an organisation with an OFFICIAL, confirmed domain, a small bounded set of approved pages is fetched (SafeFetcher +
robots + RateLimiter, same-site only) and outbound COMPANY links / structured list items are extracted
deterministically. Each candidate goes through :func:`resolution.resolving_upsert_ex` (adapter ``network_expansion``)
and is joined to the seed by a ``BusinessRelationship`` carrying depth, source URL and confidence. A discovered company
is only ever a *candidate*: its website stays an unverified candidate domain and a ``resolve_domain`` job is queued, so
identity resolution decides - this module never confirms a domain.

Traversal is bounded by ``max_depth`` (<= 3), a total fetch ``budget``, a per-site page cap, a per-page candidate cap
and a visited set (cycle safe). Depth > 1 only continues through organisations that ALREADY have a confirmed domain.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from ..crawler.ratelimit import RateLimiter
from ..crawler.robots import Decision, RobotsCache
from ..crawler.safe_http import SafeFetcher
from ..crawler.urls import registrable_domain, strip_www
from ..models import BusinessRelationship, Organisation, OrganisationDomain
from ..pipeline.identity import is_intermediary_domain
from ..sources.base import BusinessRecord
from ..sources.persist import normalise_name
from ..sources.urls import normalise_website
from .htmlutil import clean, jsonld_nodes, parse, text_of
from .resolution import _upsert_relationship, resolving_upsert_ex

ADAPTER = "network_expansion"
HARD_NODE_CAP = 300
MAX_DEPTH = 3

# page kind -> (URL/anchor regex, relation, candidate_is_from)
PAGE_KINDS: list[tuple[str, re.Pattern[str], str, bool]] = [
    ("franchise", re.compile(r"franchis", re.I), "franchise_of", True),
    ("suppliers", re.compile(r"supplier|vendor", re.I), "supplier_of", True),
    ("partners", re.compile(r"partner|affiliat|alliance|collaborat|associat", re.I), "partner_of", False),
    ("brands", re.compile(r"brands?\b|our-brands|manufacturer|marques|stockist", re.I), "brand_of", True),
    ("members", re.compile(r"member|our-network|network", re.I), "member_of_group", True),
    ("locations", re.compile(r"locations?\b|branches|branch-finder|find-us|our-offices|offices\b|stores\b|depots?\b|showrooms?\b|clinics?\b|studios?\b", re.I),
     "branch_of", True),
    ("group", re.compile(r"our-group|group-companies|subsidiar|sister-compan|companies\b", re.I), "subsidiary_of", True),
]
_KIND = {k: (rx, rel, cf) for k, rx, rel, cf in PAGE_KINDS}
_NEGATIVE = re.compile(r"login|log-in|signin|sign-in|cart|basket|checkout|account|wp-admin|/tag/|/category/|/feed|/search|logout|"
                       r"privacy|cookie|terms|\.pdf$|\.zip$|\.jpe?g$|\.png$|replytocom", re.I)
_CONTAINER = re.compile(r"partner|supplier|member|brand|client|affiliat|network|branch|location|franchise|logo|accredit|stockist|group", re.I)
_BOILERPLATE_CLASS = re.compile(r"cookie|menu|social|share|breadcrumb|pagination|newsletter|skip", re.I)
_GENERIC_TEXT = re.compile(
    r"^(?:click here|here|read more|learn more|visit(?: website| site| us)?|website|web site|site|link|more|view|view more|"
    r"go to site|find out more|www\..*|https?://.*|home|contact|contact us|about|about us|next|previous|apply|join|login)$", re.I)
_EXTRA_AGGREGATORS = frozenset({
    "wixsite.com", "wordpress.com", "blogspot.com", "squarespace.com", "shopify.com", "myshopify.com", "weebly.com", "godaddy.com",
    "w3.org", "schema.org", "paypal.com", "visa.com", "mastercard.com", "stripe.com", "cloudflare.com", "gov.uk", "europa.eu",
    "mailchimp.com", "eventbrite.com", "eventbrite.co.uk", "vimeo.com", "flickr.com", "tumblr.com", "wa.me", "goo.gl",
    "apple.com", "play.google.com", "microsoft.com", "adobe.com", "wikipedia.org", "bbc.co.uk", "theguardian.com",
    "companieshouse.gov.uk", "trustpilot.com", "reviews.io", "feefo.com", "yell.com", "checkatrade.com", "bark.com",
})


@dataclass
class Candidate:
    name: str
    url: str | None  # candidate website (normalised) or None for a branch item
    relation: str
    candidate_is_from: bool
    confidence: float
    method: str
    page_url: str
    page_kind: str
    is_branch: bool = False


@dataclass
class NetworkResult:
    seed_org_id: int
    fetched: int = 0
    pages: list[dict[str, Any]] = field(default_factory=list)
    new_orgs: list[int] = field(default_factory=list)
    matched_orgs: list[int] = field(default_factory=list)
    edges: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    jobs_enqueued: int = 0
    budget_exhausted: bool = False
    depth_reached: int = 0
    visited_orgs: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- extraction
def is_rejected_domain(host_or_url: str | None, seed_hosts: set[str] | None = None) -> str | None:
    """Reason a link target can NOT be a candidate company website (aggregator / social / directory / same site ...)."""
    if not host_or_url:
        return "empty"
    sp = urlsplit(host_or_url if "://" in host_or_url else f"//{host_or_url}")
    if sp.scheme not in ("", "http", "https"):
        return "not_http"
    host = strip_www((sp.hostname or "").lower())
    if not host:
        return "no_host"
    reg = registrable_domain(host)
    if not reg:
        return "no_registrable_domain"
    if is_intermediary_domain(host):
        return "aggregator_or_social"
    if reg in _EXTRA_AGGREGATORS or any(host.endswith("." + d) for d in _EXTRA_AGGREGATORS):
        return "aggregator_or_platform"
    if seed_hosts and (host in seed_hosts or reg in {registrable_domain(h) for h in seed_hosts}):
        return "same_site"
    return None


def candidate_name(text_: str | None) -> str | None:
    t = clean(text_)
    if not t or _GENERIC_TEXT.match(t) or not re.search(r"[A-Za-z]", t):
        return None
    if "@" in t or re.search(r"\d{5,}", t) or len(t) > 80 or len(t) < 2 or len(t.split()) > 8:
        return None
    return t


def classify_page(url: str, anchor_text: str = "") -> tuple[str, int] | None:
    """Best page kind for a same-site link by URL pattern (weight 3) and anchor text (weight 2)."""
    if _NEGATIVE.search(url):
        return None
    path = urlsplit(url).path.lower()
    best: tuple[str, int] | None = None
    for kind, rx, _rel, _cf in PAGE_KINDS:
        sc = (3 if rx.search(path) else 0) + (2 if anchor_text and rx.search(anchor_text) else 0)
        if sc and (best is None or sc > best[1]):
            best = (kind, sc)
    return best


def rank_site_links(html: str, base_url: str, limit: int = 10) -> list[tuple[str, str, int]]:
    """Same-site links ranked by URL pattern + link text: ``[(url, kind, score)]`` best first."""
    tree = parse(html)
    if tree is None:
        return []
    base_host = strip_www(urlsplit(base_url).netloc.lower())
    best: dict[str, tuple[str, int]] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        sp = urlsplit(url)
        if sp.scheme not in ("http", "https") or strip_www(sp.netloc.lower()) != base_host:
            continue
        cls = classify_page(url, text_of(a, 80))
        if cls and (url not in best or cls[1] > best[url][1]):
            best[url] = cls
    ranked = sorted(((u, k, s) for u, (k, s) in best.items()), key=lambda t: (-t[2], len(t[0]), t[0]))
    return ranked[:limit]


def _ancestors(node, depth: int = 7):
    n = node.parent
    for _ in range(depth):
        if n is None:
            return
        yield n
        n = n.parent


def _attr_blob(n) -> str:
    a = n.attributes
    return f"{a.get('class') or ''} {a.get('id') or ''}"


def extract_candidates(html: str, page_url: str, page_kind: str, seed_hosts: set[str], max_candidates: int = 15) -> list[Candidate]:
    """Deterministic candidates from one approved page (JSON-LD relations, structured lists, outbound company links)."""
    rx, relation, cand_from = _KIND[page_kind]
    tree = parse(html)
    out: list[Candidate] = []
    seen: set[str] = set()
    if tree is None:
        return out

    def add(c: Candidate, key: str) -> None:
        if key not in seen and len(out) < max_candidates:
            seen.add(key)
            out.append(c)

    # 1. JSON-LD relations ----------------------------------------------------------------
    jsonld_keys = {"member": ("member_of_group", True), "subOrganization": ("subsidiary_of", True), "brand": ("brand_of", True),
                   "memberOf": ("member_of_group", False), "parentOrganization": ("subsidiary_of", False),
                   "affiliation": ("partner_of", False), "department": ("branch_of", True)}
    for node in jsonld_nodes(html, tree):
        for key, (rel, from_cand) in jsonld_keys.items():
            for v in node.get(key) if isinstance(node.get(key), list) else ([node.get(key)] if node.get(key) else []):
                if not isinstance(v, dict):
                    continue
                name = candidate_name(str(v.get("name") or v.get("legalName") or ""))
                if not name:
                    continue
                url = str(v.get("url") or "")
                site = normalise_website(url) if url else None
                if site is not None and is_rejected_domain(site.host, seed_hosts):
                    continue
                if site is None and key not in ("department", "subOrganization"):
                    continue  # a company reference without a website is not actionable
                add(Candidate(name, site.url if site else None, rel, from_cand, 0.7, "jsonld", page_url, page_kind,
                              is_branch=site is None and rel == "branch_of"),
                    site.registrable if site else "b:" + normalise_name(name))

    # 2. outbound links in structured containers / on partner-like pages ----------------------
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href.startswith(("http://", "https://", "//")):
            continue
        anc = list(_ancestors(a))
        if any(x.tag in ("nav", "header", "footer") or _BOILERPLATE_CLASS.search(_attr_blob(x)) for x in anc):
            continue
        site = normalise_website(urljoin(page_url, href))
        if site is None or is_rejected_domain(site.host, seed_hosts):
            continue
        img = a.css_first("img")
        name = candidate_name(text_of(a, 100)) or (candidate_name(img.attributes.get("alt")) if img is not None else None) or \
            candidate_name(a.attributes.get("title"))
        if not name:
            continue
        structured = any(_CONTAINER.search(_attr_blob(x)) for x in anc[:6]) or any(x.tag in ("li",) for x in anc[:2])
        conf = 0.6 if structured else 0.4
        add(Candidate(name, site.url, relation, cand_from, conf, "structured_list" if structured else "outbound_link", page_url, page_kind),
            site.registrable)

    # 3. branch / location list items (no website) ------------------------------------------
    if page_kind == "locations":
        for ul in tree.css("ul, ol"):
            if not _CONTAINER.search(_attr_blob(ul)) and not any(_CONTAINER.search(_attr_blob(x)) for x in _ancestors(ul, 3)):
                continue
            if any(x.tag in ("nav", "header", "footer") for x in _ancestors(ul)):
                continue
            for li in ul.css("li")[:30]:
                t = text_of(li, 120)
                if (t and 2 <= len(t) <= 80 and len(t.split()) <= 8 and not re.search(r"[.!?]\s*$|@|https?:", t)
                        and not li.css_first("a[href^=http]")):
                    add(Candidate(t, None, "branch_of", True, 0.4, "location_list", page_url, page_kind, is_branch=True), "b:" + normalise_name(t))
    return out


# --------------------------------------------------------------------------- expansion
def _seed_hosts(domain_rows: list[OrganisationDomain]) -> set[str]:
    hosts = set()
    for d in domain_rows:
        hosts.add(strip_www(d.domain.lower()))
        if d.website_url:
            hosts.add(strip_www(urlsplit(d.website_url).netloc.lower()))
    return hosts


async def _official_domain(session: AsyncSession, org_id: int) -> OrganisationDomain | None:
    return (await session.execute(select(OrganisationDomain).where(
        OrganisationDomain.organisation_id == org_id, OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
        OrganisationDomain.parked.is_(False)).order_by(OrganisationDomain.confidence.desc(), OrganisationDomain.id).limit(1))).scalar_one_or_none()


async def _fetch_page(fetcher: SafeFetcher, limiter: RateLimiter, robots: RobotsCache, url: str, res: NetworkResult, budget: int):
    """One polite fetch; returns html or None. Counts against the shared budget."""
    if res.fetched >= budget:
        res.budget_exhausted = True
        return None
    chk = await robots.check(url)
    if chk.decision is not Decision.ALLOWED:
        res.skipped.append({"url": url, "reason": "robots_" + chk.decision.value})
        return None
    host = urlsplit(url).netloc
    async with limiter.acquire(host) as slot:
        r = await fetcher.fetch(url)
        slot.report(r.status, r.retry_after() if r.status in (429, 503) else None)
    res.fetched += 1
    if not r.usable_body or (r.content_type and "html" not in r.content_type):
        res.skipped.append({"url": url, "reason": r.error_code or f"status_{r.status}"})
        return None
    return r.text(), r.final_url or url


async def expand_network(
    session: AsyncSession, fetcher: SafeFetcher, org_id: int, *, run_id: int | None, max_depth: int = 1, budget: int = 50,
    limiter: RateLimiter | None = None, robots: RobotsCache | None = None, max_pages_per_site: int = 5,
    max_candidates_per_page: int = 15, max_total_orgs: int = 100, enqueue: bool = True,
) -> NetworkResult:
    """Bounded network expansion around ``org_id``. Never raises for fetch problems; the caller owns the transaction."""
    max_depth = max(1, min(int(max_depth), MAX_DEPTH))
    limiter = limiter or RateLimiter(min_interval=1.0)
    robots = robots or RobotsCache(fetcher)
    res = NetworkResult(seed_org_id=org_id)
    visited: set[int] = set()
    queue: deque[tuple[int, int]] = deque([(org_id, 0)])
    created_total = 0
    while queue:
        cur, depth = queue.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        res.visited_orgs.append(cur)
        if depth >= max_depth:
            continue
        if res.fetched >= budget:
            res.budget_exhausted = True
            break
        dom = await _official_domain(session, cur)
        org = await session.get(Organisation, cur)
        if org is None or org.merged_into_id is not None:
            res.skipped.append({"org_id": cur, "reason": "missing_or_merged"})
            continue
        if dom is None:
            res.skipped.append({"org_id": cur, "reason": "no_confirmed_domain"})
            continue
        all_doms = list((await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == cur))).scalars())
        seed_hosts = _seed_hosts(all_doms)
        home = dom.website_url or f"https://{dom.domain}/"
        got = await _fetch_page(fetcher, limiter, robots, home, res, budget)
        if got is None:
            continue
        home_html, home_final = got
        res.pages.append({"org_id": cur, "url": home_final, "kind": "home", "depth": depth})
        ranked = rank_site_links(home_html, home_final, limit=max_pages_per_site)
        cands: list[Candidate] = []
        for url, kind, _score in ranked[:max_pages_per_site]:
            if res.fetched >= budget:
                res.budget_exhausted = True
                break
            pg = await _fetch_page(fetcher, limiter, robots, url, res, budget)
            if pg is None:
                continue
            html, final = pg
            if strip_www(urlsplit(final).netloc.lower()) != strip_www(urlsplit(home_final).netloc.lower()):
                res.skipped.append({"url": url, "reason": "redirected_off_site"})
                continue
            res.pages.append({"org_id": cur, "url": final, "kind": kind, "depth": depth})
            cands += extract_candidates(html, final, kind, seed_hosts, max_candidates_per_page)
        # ---- persist candidates (resolution decides identity; never auto-confirm a domain)
        seen_keys: set[str] = set()
        for c in cands:
            key = (registrable_domain(c.url) if c.url else "b:" + normalise_name(c.name)) or c.name
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if created_total >= max_total_orgs:
                res.skipped.append({"name": c.name, "reason": "max_total_orgs"})
                continue
            site = normalise_website(c.url) if c.url else None
            raw: dict[str, Any] = {"seed_org_id": cur, "depth": depth + 1, "relation": c.relation, "page_kind": c.page_kind,
                                   "method": c.method, "page_url": c.page_url}
            if c.is_branch:
                raw.update(entity_role="branch", parent_org_id=cur, location_text=c.name)
                rec = BusinessRecord(
                    source_ref=f"{cur}:branch:{normalise_name(c.name)}"[:300], legal_name=f"{org.legal_name or org.trading_name} - {c.name}"[:400],
                    country_code=org.country_code, address=c.name, adapter_id=ADAPTER, source_url=c.page_url, raw=raw)
            else:
                assert site is not None
                clash = await _name_clash(session, c.name, site.registrable, org.country_code)
                if clash:
                    res.skipped.append({"name": c.name, "reason": "name_collision_different_domain", "org_id": clash})
                    continue
                rec = BusinessRecord(
                    source_ref=f"{cur}:{site.registrable}"[:300], trading_name=c.name, country_code=org.country_code,
                    website=site.url, adapter_id=ADAPTER, source_url=c.page_url, raw=raw)
            cid, created, resolution = await resolving_upsert_ex(session, rec, ADAPTER, run_id)
            if cid == cur:
                continue
            (res.new_orgs if created else res.matched_orgs).append(cid)
            created_total += 1 if created else 0
            frm, to = (cid, cur) if c.candidate_is_from else (cur, cid)
            await _upsert_relationship(session, frm, to, c.relation, ADAPTER, c.page_url, depth=depth + 1, confidence=c.confidence)
            res.edges += 1
            res.depth_reached = max(res.depth_reached, depth + 1)
            if enqueue and run_id is not None and not c.is_branch:
                if await jq.enqueue(session, run_id=run_id, stage="resolve_domain", dedupe_key=f"net:{cid}",
                                    payload={"organisation_id": cid, "source": ADAPTER, "seed_org_id": cur,
                                             "candidate_domain": site.host if site else None, "depth": depth + 1},
                                    priority=80, host_key=site.host if site else None):
                    res.jobs_enqueued += 1
            if cid not in visited and depth + 1 < max_depth:
                queue.append((cid, depth + 1))
    return res


async def _name_clash(session: AsyncSession, name: str, registrable: str, country: str) -> int | None:
    """An organisation with the SAME normalised name but a different confirmed/high official domain: do not merge by name."""
    n = normalise_name(name)
    if not n:
        return None
    first = n.split()[0]
    rows = (await session.execute(select(Organisation).where(
        Organisation.country_code == country, Organisation.merged_into_id.is_(None),
        or_(Organisation.legal_name.ilike(first + "%"), Organisation.trading_name.ilike(first + "%"))).limit(200))).scalars()
    for o in rows:
        if normalise_name(o.legal_name) == n or normalise_name(o.trading_name) == n:
            d = await _official_domain(session, o.id)
            if d is not None and d.registrable_domain != registrable:
                return o.id
    return None


# --------------------------------------------------------------------------- graph queries
async def neighbourhood(session: AsyncSession, org_id: int, depth: int = 1, limit: int = 100,
                        relations: list[str] | None = None) -> dict[str, Any]:
    """Bounded neighbourhood for the visual graph: ``{nodes, edges, truncated}``. Hard cap 300 nodes, depth <= 3."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    limit = max(1, min(int(limit), HARD_NODE_CAP))
    dist: dict[int, int] = {org_id: 0}
    truncated = False
    frontier = [org_id]
    for d in range(1, depth + 1):
        if not frontier:
            break
        q = select(BusinessRelationship).where(or_(BusinessRelationship.from_org_id.in_(frontier), BusinessRelationship.to_org_id.in_(frontier)))
        if relations:
            q = q.where(BusinessRelationship.relation.in_(relations))
        rows = (await session.execute(q.order_by(BusinessRelationship.confidence.desc(), BusinessRelationship.id).limit(limit * 4))).scalars().all()
        nxt: list[int] = []
        for r in rows:
            for o in (r.from_org_id, r.to_org_id):
                if o not in dist:
                    if len(dist) >= limit:
                        truncated = True
                        continue
                    dist[o] = d
                    nxt.append(o)
        frontier = nxt
    ids = list(dist)
    eq = select(BusinessRelationship).where(BusinessRelationship.from_org_id.in_(ids), BusinessRelationship.to_org_id.in_(ids))
    if relations:
        eq = eq.where(BusinessRelationship.relation.in_(relations))
    edges = (await session.execute(eq.order_by(BusinessRelationship.id).limit(1000))).scalars().all()
    orgs = {o.id: o for o in (await session.execute(select(Organisation).where(Organisation.id.in_(ids)))).scalars()}
    confirmed = set((await session.execute(select(OrganisationDomain.organisation_id).where(
        OrganisationDomain.organisation_id.in_(ids), OrganisationDomain.verified_match_status.in_(("confirmed", "high"))))).scalars())
    nodes = [{"id": i, "name": (orgs[i].legal_name or orgs[i].trading_name) if i in orgs else None, "country_code": orgs[i].country_code if i in orgs else None,
              "city": orgs[i].city if i in orgs else None, "distance": dist[i], "has_confirmed_domain": i in confirmed,
              "merged_into_id": orgs[i].merged_into_id if i in orgs else None} for i in sorted(ids, key=lambda x: (dist[x], x))]
    return {"nodes": nodes, "edges": [_edge(e) for e in edges], "truncated": truncated, "center": org_id}


def _edge(e: BusinessRelationship) -> dict[str, Any]:
    return {"id": e.id, "from": e.from_org_id, "to": e.to_org_id, "relation": e.relation, "depth": e.depth, "confidence": e.confidence,
            "source_adapter": e.source_adapter, "source_url": e.source_url, "observed_at": e.observed_at}


async def list_relationships(session: AsyncSession, filters: dict[str, Any] | None = None, cursor: int | None = None, limit: int = 50) -> dict[str, Any]:
    """Table fallback for the graph: keyset-paginated (``id`` descending). filters: org_id, relation, source_adapter, min_confidence, max_depth."""
    f = filters or {}
    limit = max(1, min(int(limit), 500))
    q = select(BusinessRelationship)
    if f.get("org_id"):
        q = q.where(or_(BusinessRelationship.from_org_id == f["org_id"], BusinessRelationship.to_org_id == f["org_id"]))
    if f.get("relation"):
        q = q.where(BusinessRelationship.relation == f["relation"])
    if f.get("source_adapter"):
        q = q.where(BusinessRelationship.source_adapter == f["source_adapter"])
    if f.get("min_confidence") is not None:
        q = q.where(BusinessRelationship.confidence >= f["min_confidence"])
    if f.get("max_depth") is not None:
        q = q.where(BusinessRelationship.depth <= f["max_depth"])
    if cursor is not None:
        q = q.where(BusinessRelationship.id < cursor)
    rows = (await session.execute(q.order_by(BusinessRelationship.id.desc()).limit(limit + 1))).scalars().all()
    more = len(rows) > limit
    rows = rows[:limit]
    ids = {x for r in rows for x in (r.from_org_id, r.to_org_id)}
    names = {o.id: (o.legal_name or o.trading_name) for o in (await session.execute(
        select(Organisation).where(Organisation.id.in_(ids)))).scalars()} if ids else {}
    items = [{**_edge(r), "from_name": names.get(r.from_org_id), "to_name": names.get(r.to_org_id)} for r in rows]
    return {"items": items, "next_cursor": rows[-1].id if more and rows else None}
