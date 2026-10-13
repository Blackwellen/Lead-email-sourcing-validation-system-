"""Domain identity resolution: is this website really that organisation's official site?

Principles
----------
* A domain *guessed* from a company name is only ever a probe candidate. Name similarity alone
  can never push it beyond ``review``; it takes independent hard evidence found ON the site
  (company number, postcode, phone, schema.org url) to rise further.
* Evidence is explicit, weighted and timestamped; the weights are combined with a noisy-OR so no
  single weak signal dominates and repeated correlated signals do not stack unboundedly.
* Parked/expired/default pages and directory/social/marketplace profiles are never "the
  official site".
* Rows flagged ``manual_override`` are never modified.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.crawl import CrawlOptions, CrawlResult, crawl_domain
from ..crawler.extract import phone_key
from ..crawler.ratelimit import RateLimiter
from ..crawler.robots import RobotsCache
from ..crawler.safe_http import SafeFetcher
from ..crawler.types import SiteEvidence
from ..crawler.urls import registrable_domain, strip_www, to_ascii_host
from ..models import Organisation, OrganisationDomain

__all__ = [
    "Candidate", "DomainMatch", "INTERMEDIARY_DOMAINS", "ResolutionResult", "candidate_domains_from_name",
    "is_intermediary_domain", "normalise_company_name", "registrable_domain", "resolve_organisation_domain",
    "score_domain_match", "same_registrable",
]

# --------------------------------------------------------------------------- names
_LEGAL_SUFFIXES = {
    "ltd", "limited", "llp", "lp", "plc", "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "gmbh", "ag", "kg", "ohg", "ug", "mbh", "sarl", "sas", "sa", "srl", "spa", "sl", "slu", "sasu", "eurl", "bv",
    "nv", "ab", "oy", "as", "aps", "pty", "pvt", "pte", "bhd", "sdn", "cic", "cio", "ltda", "limitada", "sprl",
    "cvba", "ek", "e.k", "ev", "se", "kft", "zrt", "sro", "spol", "doo", "ood", "ad", "oü", "ou", "sia", "uab",
    "ltd.", "holdings", "group",
}
_LEADING = {"the"}
_AMP = re.compile(r"\s*&\s*")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_STRIP_SUFFIX_KEEP = {"holdings", "group"}  # only stripped when something else remains


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def name_tokens(name: str | None, *, strip_legal: bool = True) -> list[str]:
    if not name:
        return []
    s = _fold(name).casefold().replace("ß", "ss")
    s = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group(0).replace(".", ""), s)  # S.A.R.L. -> sarl
    s = _AMP.sub(" and ", s)
    s = s.replace("'", "").replace("’", "")
    toks = [t for t in _NON_ALNUM.split(s) if t]
    if strip_legal:
        while len(toks) > 1 and toks[-1] in _LEGAL_SUFFIXES:
            toks.pop()
        # "t/a", "trading as" fragments are cut by the caller; leading article
        while len(toks) > 1 and toks[0] in _LEADING:
            toks.pop(0)
        # "Smith and Sons (UK) Limited": a lone trailing "uk" is noise only when other tokens remain
        if len(toks) > 2 and toks[-1] in ("uk", "gb", "europe", "international", "intl"):
            toks.pop()
    return toks


def normalise_company_name(name: str | None) -> str:
    """Canonical comparison form: unicode-folded, case-folded, punctuation removed, legal suffix stripped."""
    return " ".join(name_tokens(name))


def _text_norm(text: str | None) -> str:
    return " ".join(name_tokens(text, strip_legal=False))


def same_registrable(a: str | None, b: str | None) -> bool:
    ra, rb = registrable_domain(a), registrable_domain(b)
    return ra is not None and ra == rb


# --------------------------------------------------------------------------- intermediaries
INTERMEDIARY_DOMAINS = frozenset({
    # social
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "pinterest.com", "snapchat.com", "reddit.com", "whatsapp.com", "t.me", "wa.me", "vk.com",
    # directories / review / listing
    "yell.com", "checkatrade.com", "trustpilot.com", "yelp.com", "tripadvisor.com", "bark.com", "trustatrader.com",
    "ratedpeople.com", "mybuilder.com", "freeindex.co.uk", "thomsonlocal.com", "192.com", "cylex.co.uk", "hotfrog.co.uk",
    "scoot.co.uk", "yellowpages.com", "yellowpages.co.uk", "foursquare.com", "manta.com", "bbb.org", "angi.com",
    "houzz.com", "treatwell.co.uk", "opentable.com", "booking.com", "expedia.com", "airbnb.com", "glassdoor.com",
    "indeed.com", "reed.co.uk", "totaljobs.com", "gumtree.com", "nextdoor.com", "yelp.co.uk", "which.co.uk",
    "vouchedfor.co.uk", "unbiased.co.uk", "lawsociety.org.uk", "sra.org.uk", "rics.org", "nhs.uk", "doctify.com",
    "topdoctors.co.uk", "dentist.co.uk", "yell.co.uk", "uk-directory.com", "find-a-dentist.com",
    # corporate data / registries (informative, not the company's own site)
    "companieshouse.gov.uk", "find-and-update.company-information.service.gov.uk", "opencorporates.com", "endole.co.uk",
    "dnb.com", "zoominfo.com", "crunchbase.com", "rocketreach.co", "apollo.io", "lusha.com", "bloomberg.com",
    "companycheck.co.uk", "company-information.service.gov.uk", "northdata.com", "kompass.com", "europages.com",
    "wikipedia.org", "wikidata.org", "google.com", "goo.gl", "g.page", "maps.app.goo.gl", "bing.com", "apple.com",
    # marketplaces / property portals
    "ebay.com", "ebay.co.uk", "amazon.com", "amazon.co.uk", "etsy.com", "alibaba.com", "aliexpress.com",
    "rightmove.co.uk", "zoopla.co.uk", "onthemarket.com", "primelocation.com", "autotrader.co.uk", "gumtree.co.uk",
    "linktr.ee", "carrd.co", "about.me", "bit.ly", "tinyurl.com", "medium.com",
})


def is_intermediary_domain(host_or_url: str | None) -> bool:
    reg = registrable_domain(host_or_url)
    if reg is None:
        return False
    if reg in INTERMEDIARY_DOMAINS:
        return True
    host = to_ascii_host((host_or_url or "").split("://")[-1].split("/")[0].split(":")[0]) or ""
    return host in INTERMEDIARY_DOMAINS or any(host.endswith("." + d) for d in INTERMEDIARY_DOMAINS)


# --------------------------------------------------------------------------- candidates
@dataclass(frozen=True, slots=True)
class Candidate:
    """A domain to be verified. ``source`` says how we came by it - only 'guess' is speculative."""

    domain: str
    source: str = "guess"  # guess | registry | directory | osm | dataset | search
    detail: str | None = None

    @property
    def speculative(self) -> bool:
        return self.source == "guess"


_CC_TLDS = {
    "GB": ["co.uk", "uk", "com", "org.uk", "org", "net"],
    "IE": ["ie", "com", "co"], "DE": ["de", "com"], "FR": ["fr", "com"], "ES": ["es", "com"], "IT": ["it", "com"],
    "NL": ["nl", "com"], "US": ["com", "net", "org"], "CA": ["ca", "com"], "AU": ["com.au", "au", "com"],
    "NZ": ["co.nz", "nz", "com"],
}


def candidate_domains_from_name(
    name: str | None,
    *,
    country_code: str = "GB",
    max_candidates: int = 10,
    extra_tlds: Iterable[str] = (),
) -> list[Candidate]:
    """Generate domains to PROBE for a company name.

    These are guesses (``source='guess'``). They must be crawled and verified with
    ``score_domain_match`` before anything is stored as an organisation's website; a guess whose
    only supporting signal is name similarity is capped at ``review``. Never store them as confirmed.
    """
    toks = name_tokens(name)
    if not toks:
        return []
    labels: list[str] = []
    joined = "".join(toks)
    hyph = "-".join(toks)
    labels.append(joined)
    if len(toks) > 1:
        labels.append(hyph)
        no_and = [t for t in toks if t != "and"]
        if no_and != toks:
            labels.append("".join(no_and))
        if len(no_and) > 2:
            labels.append("".join(no_and[:2]))
    labels = [lab for lab in dict.fromkeys(labels) if 2 <= len(lab) <= 63 and not lab.startswith("-")]
    tlds = list(dict.fromkeys([*extra_tlds, *_CC_TLDS.get((country_code or "").upper(), ["com"])]))
    out: list[Candidate] = []
    for tld in tlds:
        for lab in labels:
            d = f"{lab}.{tld}"
            if registrable_domain(d) and not is_intermediary_domain(d):
                out.append(Candidate(d, "guess", f"name-derived from {name!r}"))
    return out[:max_candidates]


# --------------------------------------------------------------------------- scoring
@dataclass(slots=True)
class DomainMatch:
    status: str  # confirmed | high | review | mismatch | unknown
    confidence: float
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def parked(self) -> bool:
        return any(e["type"] == "parked" for e in self.evidence)


_W = {
    "company_number_match": 0.92,
    "postcode_match": 0.42,
    "address_match": 0.12,
    "phone_match": 0.45,
    "schema_org_url": 0.15,
    "site_name_meta": 0.12,
    "domain_name_similarity": 0.10,
}
_SOURCE_W = {"registry": 0.55, "directory": 0.25, "osm": 0.22, "dataset": 0.25, "search": 0.12, "guess": 0.0}
_NAME_W = {"title": (0.30, 0.17), "h1": (0.25, 0.14), "footer": (0.30, 0.15), "schema": (0.32, 0.18), "text": (0.12, 0.06)}


def _cn_key(v: str | None) -> str:
    s = re.sub(r"[^0-9A-Za-z]", "", v or "").upper()
    return s.lstrip("0") if s.isdigit() else s


def _compact(s: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", _fold(s or "")).upper()


def _name_level(name_tokens_: list[str], hay_norm: str) -> int:
    """2 = full phrase present, 1 = most distinctive tokens present, 0 = none."""
    if not name_tokens_ or not hay_norm:
        return 0
    phrase = " ".join(name_tokens_)
    padded = f" {hay_norm} "
    if f" {phrase} " in padded:
        return 2
    if len(name_tokens_) >= 2:
        htoks = set(hay_norm.split())
        cover = sum(1 for t in name_tokens_ if t in htoks) / len(name_tokens_)
        if cover >= 0.75:
            return 1
    return 0


def _label_similarity(domain: str | None, toks: list[str]) -> bool:
    reg = registrable_domain(domain)
    if not reg or not toks:
        return False
    label = reg.split(".")[0].replace("-", "")
    joined = "".join(toks)
    acronym = "".join(t[0] for t in toks) if len(toks) >= 3 else ""
    return label == joined or (len(joined) >= 5 and joined in label and len(label) <= len(joined) + 6) or (
        bool(acronym) and len(acronym) >= 3 and label == acronym)


def score_domain_match(org: Any, site: SiteEvidence, *, now: datetime | None = None) -> DomainMatch:
    """Score how strongly the evidence found on ``site`` ties the domain to ``org``.

    ``org`` is any object with legal_name / trading_name / company_number / postal_code / phone / address.
    """
    ts = (now or datetime.now(UTC)).isoformat()
    ev: list[dict[str, Any]] = []

    def add(type_: str, detail: str, weight: float) -> None:
        ev.append({"type": type_, "detail": detail, "weight": round(weight, 3), "observed_at": ts})

    domain = site.domain
    # ---- disqualifiers first
    if is_intermediary_domain(domain) or (site.final_url and is_intermediary_domain(site.final_url)):
        add("intermediary_domain", f"{domain} is a directory/social/marketplace/registry domain, not an official site", 0.0)
        return DomainMatch("mismatch", 0.0, ev)
    if site.parked:
        add("parked", f"parked/placeholder content detected ({site.parked})", 0.0)
        return DomainMatch("mismatch", 0.0, ev)
    if site.redirected_domain and is_intermediary_domain(site.redirected_domain):
        add("intermediary_domain", f"redirects to {site.redirected_domain}", 0.0)
        return DomainMatch("mismatch", 0.0, ev)
    source = site.source_kind or "guess"
    if not site.reachable:
        reason = "dns_failure" if site.dns_failure else "tls_failure" if site.tls_failure else "unreachable"
        add("unreachable", f"site could not be fetched ({reason}); nothing verified", 0.0)
        return DomainMatch("unknown", 0.0, ev)

    names = [n for n in (getattr(org, "legal_name", None), getattr(org, "trading_name", None)) if n]
    name_toks = [t for t in (name_tokens(n) for n in names) if t]
    has_names = bool(name_toks)
    positives: dict[str, float] = {}
    hard: set[str] = set()

    # ---- company number (strong)
    org_cn = _cn_key(getattr(org, "company_number", None))
    site_cns = {_cn_key(c) for c in site.company_numbers if c}
    cn_conflict = False
    if org_cn and org_cn in site_cns:
        add("company_number_match", f"company number {getattr(org, 'company_number', '')} published on site", _W["company_number_match"])
        positives["company_number_match"] = _W["company_number_match"]
        hard.add("company_number_match")
    elif org_cn and site_cns:
        cn_conflict = True
        add("company_number_conflict", f"site publishes company number(s) {sorted(site_cns)}, expected {org_cn}", 0.0)

    # ---- name signals (correlated -> grouped)
    name_hits: list[tuple[str, int, float]] = []

    def probe(where: str, texts: Iterable[str]) -> None:
        best = 0
        for t in texts:
            tn = _text_norm(t)
            for nt in name_toks:
                best = max(best, _name_level(nt, tn))
        if best:
            w = _NAME_W[where][0 if best == 2 else 1]
            name_hits.append((where, best, w))
            add(f"name_in_{where}", f"{'full' if best == 2 else 'partial'} organisation name match in {where}", w)

    probe("title", site.titles)
    probe("h1", site.h1s)
    probe("footer", site.footer_texts)
    schema_names: list[str] = []
    for om in site.org:
        schema_names += [x for x in (om.name, om.legal_name) if x]
    probe("schema", schema_names)
    if site.site_names:
        sl = max((_name_level(nt, _text_norm(sn)) for sn in site.site_names for nt in name_toks), default=0)
        if sl:
            positives["site_name_meta"] = _W["site_name_meta"]
            add("site_name_meta", "og:site_name / application-name matches", _W["site_name_meta"])
    probe("text", site.text_samples)
    if name_hits:
        ws = sorted((w for _, _, w in name_hits), reverse=True)
        positives["name_group"] = min(0.5, ws[0] + 0.08 * (len(ws) - 1))
    has_name = bool(name_hits)
    strong_name = any(lvl == 2 and where in ("title", "h1", "footer", "schema") for where, lvl, _ in name_hits)

    # ---- schema.org organisation facts
    reg = registrable_domain(domain)
    for om in site.org:
        if om.url and reg and registrable_domain(om.url) == reg and (
                not name_toks or _schema_names_match(om, name_toks) or not (om.name or om.legal_name)):
            positives["schema_org_url"] = _W["schema_org_url"]
            add("schema_org_url", f"schema.org {om.schema_type or 'Organization'} url is {om.url}", _W["schema_org_url"])
            hard.add("schema_org_url")
            break
    for om in site.org:
        if om.company_number and org_cn and _cn_key(om.company_number) == org_cn and "company_number_match" not in positives:
            positives["company_number_match"] = _W["company_number_match"]
            add("company_number_match", "company number in schema.org identifier", _W["company_number_match"])
            hard.add("company_number_match")
            cn_conflict = False

    # ---- location & phone
    org_pc = _compact(getattr(org, "postal_code", None))
    hay_compact = _compact(" ".join(site.footer_texts + site.text_samples))
    site_pcs = {_compact(p) for p in site.postcodes}
    site_pcs |= {_compact(om.postcode) for om in site.org if om.postcode}
    if org_pc and len(org_pc) >= 5 and (org_pc in site_pcs or org_pc in hay_compact):
        positives["postcode_match"] = _W["postcode_match"]
        add("postcode_match", f"postcode {getattr(org, 'postal_code', '')} published on site", _W["postcode_match"])
        hard.add("postcode_match")
    street = _street_key(getattr(org, "address", None))
    if street and street in _text_norm(" ".join(site.footer_texts + site.text_samples) + " " + " ".join(
            f"{om.street or ''}" for om in site.org)):
        positives["address_match"] = _W["address_match"]
        add("address_match", f"street address fragment '{street}' published on site", _W["address_match"])
    org_phone = phone_key(getattr(org, "phone", None))
    site_phones = {phone_key(p) for p in site.phones} | {phone_key(p) for om in site.org for p in om.telephone}
    site_phones.discard(None)
    if org_phone and org_phone in site_phones:
        positives["phone_match"] = _W["phone_match"]
        add("phone_match", "organisation telephone number published on site", _W["phone_match"])
        hard.add("phone_match")

    # ---- provenance of the candidate
    if source != "guess":
        w = _SOURCE_W.get(source, 0.12)
        positives["candidate_source"] = w
        add("directory_reference" if source != "registry" else "registry_reference",
            f"domain supplied by '{source}' source", w)
        if source == "registry":
            hard.add("registry_reference")
    if has_names and _label_similarity(domain, name_toks[0]):
        positives["domain_name_similarity"] = _W["domain_name_similarity"]
        add("domain_name_similarity", "domain label resembles the organisation name (weak)", _W["domain_name_similarity"])
    if site.redirected_domain:
        add("redirect", f"{domain} redirects to {site.redirected_domain}", 0.0)

    # ---- combine (noisy-OR)
    miss = 1.0
    for w in positives.values():
        miss *= (1.0 - w)
    conf = round(1.0 - miss, 3)

    # ---- decide
    has_cn = "company_number_match" in hard
    if has_cn and not cn_conflict:
        status = "confirmed"
    elif len(hard) >= 2 and has_name and conf >= 0.85:
        status = "confirmed"
    elif (len(hard) >= 2 and has_name) or (len(hard) >= 1 and strong_name and conf >= 0.7):
        status = "high"
    elif conf >= 0.25 or has_name:
        status = "review"
    else:
        status = "unknown"

    # ---- caps (never let weak evidence masquerade as strong)
    if source == "guess" and not (hard - {"schema_org_url"}) and status in ("confirmed", "high"):
        status = "review"
        add("cap", "guessed domain with only name/similarity evidence is capped at 'review'", 0.0)
    if cn_conflict and status in ("confirmed", "high"):
        status = "review"
    if site.redirected_domain and status == "confirmed" and source == "guess":
        status = "high"
        add("cap", "guessed domain redirects elsewhere; capped at 'high'", 0.0)
    if cn_conflict and conf < 0.5:
        status = "mismatch"
    if status == "unknown" and has_names and source == "guess" and sum(len(t) for t in site.text_samples) > 300 and not (set(positives) - {"domain_name_similarity"}):
        status = "mismatch"
        add("no_organisation_evidence", "reachable guessed site shows no trace of the organisation", 0.0)
    if status == "review":
        conf = min(conf, 0.69)
    elif status == "high":
        conf = min(max(conf, 0.7), 0.89)
    elif status == "confirmed":
        conf = max(conf, 0.9)
    elif status in ("mismatch", "unknown"):
        conf = min(conf, 0.2)
    return DomainMatch(status, round(conf, 3), ev)


def _schema_names_match(om, name_toks: list[list[str]]) -> bool:
    for n in (om.name, om.legal_name):
        tn = _text_norm(n)
        if tn and any(_name_level(nt, tn) for nt in name_toks):
            return True
    return False


def _street_key(address: str | None) -> str | None:
    """First address line (house number + street) in comparable form, if distinctive."""
    if not address:
        return None
    first = re.split(r"[,\n;]", address.strip())[0]
    k = _text_norm(first)
    return k if len(k) >= 6 and re.search(r"[a-z]", k) and re.search(r"\d", k) else None


# --------------------------------------------------------------------------- persistence
@dataclass(slots=True)
class ResolutionResult:
    best: OrganisationDomain | None = None
    rows: list[tuple[OrganisationDomain, DomainMatch | None]] = field(default_factory=list)
    crawls: dict[str, CrawlResult] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)


_RANK = {"confirmed": 4, "high": 3, "review": 2, "unknown": 1, "mismatch": 0}


def _as_candidate(c: str | Candidate) -> Candidate | None:
    if isinstance(c, Candidate):
        d = c.domain
        cand = c
    else:
        d = c
        cand = Candidate(domain=c)
    d = d.strip().split("://")[-1].split("/")[0].split(":")[0].strip().lower()
    if not to_ascii_host(d) or not registrable_domain(d):
        return None
    return Candidate(strip_www(to_ascii_host(d) or d), cand.source, cand.detail)


async def _upsert_domain(session: AsyncSession, org_id: int, cand: Candidate, match: DomainMatch, *,
                         website_url: str | None, parked: bool, now: datetime, crawled: bool) -> OrganisationDomain:
    vals = {
        "organisation_id": org_id, "domain": cand.domain, "registrable_domain": registrable_domain(cand.domain),
        "website_url": website_url, "verified_match_status": match.status, "confidence": match.confidence,
        "evidence": match.evidence, "parked": parked, "last_checked_at": now,
    }
    if crawled:
        vals["last_crawled_at"] = now
    stmt = pg_insert(OrganisationDomain).values(**vals)
    upd = {k: v for k, v in vals.items() if k not in ("organisation_id", "domain")}
    stmt = stmt.on_conflict_do_update(
        constraint="uq_orgdomain", set_=upd, where=OrganisationDomain.manual_override.is_(False))
    await session.execute(stmt)
    row = (await session.execute(select(OrganisationDomain).where(
        OrganisationDomain.organisation_id == org_id, OrganisationDomain.domain == cand.domain
    ).execution_options(populate_existing=True))).scalar_one()
    return row


async def resolve_organisation_domain(
    session: AsyncSession,
    fetcher: SafeFetcher,
    org_id: int,
    candidates: Sequence[str | Candidate],
    *,
    crawl_options: CrawlOptions | None = None,
    limiter: RateLimiter | None = None,
    robots: RobotsCache | None = None,
    max_candidates: int = 5,
    stop_when_confirmed: bool = True,
    now: datetime | None = None,
) -> ResolutionResult:
    """Lightly crawl candidate sites, score the evidence and upsert ``OrganisationDomain`` rows.

    * rows with ``manual_override`` are returned untouched (no crawl, no write);
    * the caller owns the transaction (this function only flushes);
    * all crawl results are returned so callers can persist e-mails without re-crawling.
    """
    org = await session.get(Organisation, org_id)
    out = ResolutionResult()
    if org is None:
        raise ValueError(f"organisation {org_id} not found")
    now = now or datetime.now(UTC)
    opts = crawl_options or CrawlOptions(
        max_pages=3, max_depth=1, max_pdfs=0, time_budget_s=30.0, use_sitemap=False, guess_standard_paths=True,
        company_hint=org.legal_name or org.trading_name)
    limiter = limiter or RateLimiter(min_interval=0.5)
    robots = robots or RobotsCache(fetcher)
    seen: set[str] = set()
    todo: list[Candidate] = []
    for c in candidates:
        cand = _as_candidate(c)
        if cand is None:
            out.skipped.append((c.domain if isinstance(c, Candidate) else str(c), "invalid_domain"))
            continue
        key = registrable_domain(cand.domain) or cand.domain
        if key in seen:
            continue
        seen.add(key)
        todo.append(cand)
    for cand in todo[:max_candidates]:
        existing = (await session.execute(select(OrganisationDomain).where(
            OrganisationDomain.organisation_id == org_id, OrganisationDomain.domain == cand.domain))).scalar_one_or_none()
        if existing is not None and existing.manual_override:
            out.rows.append((existing, None))
            out.skipped.append((cand.domain, "manual_override"))
            continue
        if is_intermediary_domain(cand.domain):
            match = score_domain_match(org, SiteEvidence(domain=cand.domain, source_kind=cand.source), now=now)
            row = await _upsert_domain(session, org_id, cand, match, website_url=None, parked=False, now=now, crawled=False)
            out.rows.append((row, match))
            continue
        crawl = await crawl_domain(fetcher, cand.domain, options=opts, limiter=limiter, robots=robots)
        out.crawls[cand.domain] = crawl
        site = crawl.site_evidence(source_kind=cand.source)
        site.domain = cand.domain
        match = score_domain_match(org, site, now=now)
        row = await _upsert_domain(
            session, org_id, cand, match, website_url=crawl.health.final_url if crawl.health.reachable else None,
            parked=bool(crawl.health.parked), now=now, crawled=True)
        out.rows.append((row, match))
        if stop_when_confirmed and match.status == "confirmed":
            break
    org.last_checked_at = now
    await session.flush()
    scored = [(r, m) for r, m in out.rows]
    if scored:
        def rank(item):
            r, _m = item
            return (r.manual_override, _RANK.get(r.verified_match_status, 0), r.confidence)

        best_row = max(scored, key=rank)[0]
        out.best = best_row if best_row.verified_match_status in ("confirmed", "high", "review") or best_row.manual_override else None
    return out
