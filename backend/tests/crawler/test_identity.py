from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.crawler.crawl import CrawlOptions
from app.crawler.ratelimit import RateLimiter
from app.crawler.safe_http import SafeFetcher
from app.crawler.types import OrgMetadata, SiteEvidence
from app.models import Organisation, OrganisationDomain
from app.pipeline.identity import (
    Candidate,
    candidate_domains_from_name,
    is_intermediary_domain,
    normalise_company_name,
    registrable_domain,
    resolve_organisation_domain,
    score_domain_match,
)

ORG = SimpleNamespace(legal_name="Acme Plumbing Limited", trading_name="Acme Plumbers", company_number="01234567",
                      postal_code="SW1A 1AA", phone="020 7946 0000", address="1 High Street, London")


def site(domain="acme-plumbing.co.uk", **kw):
    kw.setdefault("reachable", True)
    kw.setdefault("final_url", f"https://{domain}/")
    return SiteEvidence(domain=domain, **kw)


# ---------------------------------------------------------------- names / domains
@pytest.mark.parametrize("raw,norm", [
    ("Acme Plumbing Ltd", "acme plumbing"),
    ("ACME PLUMBING LIMITED.", "acme plumbing"),
    ("The Acme Plumbing Co. Ltd", "acme plumbing"),
    ("Smith & Sons LLP", "smith and sons"),
    ("Smith and Sons PLC", "smith and sons"),
    ("Müller Bäckerei GmbH", "muller backerei"),
    ("Société Générale S.A.R.L.", "societe generale"),
    ("O'Brien's Pub Inc.", "obriens pub"),
    ("Ltd", "ltd"),
    ("", ""),
    (None, ""),
    ("Café Rouge (UK) Limited", "cafe rouge"),
])
def test_normalise_company_name(raw, norm):
    assert normalise_company_name(raw) == norm


@pytest.mark.parametrize("value,reg", [
    ("www.Example.co.uk", "example.co.uk"), ("https://shop.example.com/path?x=1", "example.com"),
    ("john@mail.example.org", "example.org"), ("münchen.de", "xn--mnchen-3ya.de"), ("WWW.BÜCHER.de", "xn--bcher-kva.de"),
    ("foo.blogspot.com", "foo.blogspot.com"), ("127.0.0.1", None), ("localhost", None), ("", None), (None, None),
    ("a.b.c.example.com.au", "example.com.au"), ("example.com:8080", "example.com"), ("http://[::1]/", None),
])
def test_registrable_domain(value, reg):
    assert registrable_domain(value) == reg


def test_www_equivalence():
    assert registrable_domain("www.acme.co.uk") == registrable_domain("acme.co.uk")


def test_candidate_domains_are_marked_as_guesses():
    c = candidate_domains_from_name("Acme Plumbing Ltd", country_code="GB")
    assert c and all(x.source == "guess" and x.speculative for x in c)
    doms = [x.domain for x in c]
    assert "acmeplumbing.co.uk" in doms and "acme-plumbing.co.uk" in doms and "acmeplumbing.com" in doms
    assert candidate_domains_from_name("") == [] and len(candidate_domains_from_name("A B C D", max_candidates=3)) <= 3
    assert not any(is_intermediary_domain(d) for d in doms)


def test_intermediary_detection():
    for d in ["facebook.com", "www.linkedin.com", "m.facebook.com", "yell.com", "checkatrade.com", "companieshouse.gov.uk",
              "https://www.trustpilot.com/review/acme.co.uk", "uk.linkedin.com", "google.com"]:
        assert is_intermediary_domain(d), d
    for d in ["acme.co.uk", "facebook-fan-club-acme.com", "mylinkedin.co.uk"]:
        assert not is_intermediary_domain(d), d


# ---------------------------------------------------------------- scoring
def test_company_number_on_site_confirms_even_for_guess():
    m = score_domain_match(ORG, site(company_numbers=["01234567"], source_kind="guess"))
    assert m.status == "confirmed" and m.confidence >= 0.9
    assert {"company_number_match"} <= {e["type"] for e in m.evidence}
    assert all({"type", "detail", "weight", "observed_at"} <= set(e) for e in m.evidence)


def test_company_number_leading_zero_insensitive():
    assert score_domain_match(ORG, site(company_numbers=["1234567"])).status == "confirmed"


def test_guess_with_only_name_signals_is_capped_at_review():
    ev = site(titles=["Acme Plumbing Limited | Boiler repair"], h1s=["Acme Plumbing"], footer_texts=["© Acme Plumbing Ltd"],
              org=[OrgMetadata(source="jsonld", name="Acme Plumbing Ltd")], site_names=["Acme Plumbing"],
              text_samples=["Acme Plumbing Ltd boilers"], source_kind="guess")
    m = score_domain_match(ORG, ev)
    assert m.status == "review" and m.confidence <= 0.69
    assert any(e["type"] == "cap" for e in m.evidence) or m.status == "review"


def test_name_similarity_only_via_domain_label_is_review_at_most():
    m = score_domain_match(ORG, site(domain="acmeplumbing.co.uk", titles=["Acme Plumbing"], source_kind="guess"))
    assert m.status == "review"


def test_postcode_phone_and_name_reach_high_or_confirmed():
    ev = site(titles=["Acme Plumbing Ltd"], postcodes=["SW1A 1AA"], phones=["+44 20 7946 0000"], source_kind="guess")
    m = score_domain_match(ORG, ev)
    assert m.status in ("high", "confirmed") and {"postcode_match", "phone_match"} <= {e["type"] for e in m.evidence}
    assert m.confidence >= 0.7


def test_postcode_alone_without_name_is_only_review():
    m = score_domain_match(ORG, site(postcodes=["SW1A 1AA"], source_kind="guess"))
    assert m.status == "review"


def test_dataset_supplied_domain_gets_directory_reference_weight():
    base = score_domain_match(ORG, site(titles=["Acme Plumbing"], source_kind="guess"))
    ref = score_domain_match(ORG, site(titles=["Acme Plumbing"], source_kind="osm"))
    assert ref.confidence > base.confidence and any(e["type"] == "directory_reference" for e in ref.evidence)


def test_registry_reference_plus_postcode_is_high():
    m = score_domain_match(ORG, site(titles=["Acme Plumbing"], postcodes=["SW1A 1AA"], source_kind="registry"))
    assert m.status in ("high", "confirmed")


def test_schema_org_url_and_name():
    ev = site(org=[OrgMetadata(source="jsonld", name="Acme Plumbing Ltd", url="https://www.acme-plumbing.co.uk/", postcode="SW1A 1AA")])
    m = score_domain_match(ORG, ev)
    types = {e["type"] for e in m.evidence}
    assert {"schema_org_url", "name_in_schema", "postcode_match"} <= types and m.status in ("high", "confirmed")


@pytest.mark.parametrize("domain", ["facebook.com", "www.linkedin.com", "yell.com", "checkatrade.com", "companieshouse.gov.uk"])
def test_intermediary_never_official_site(domain):
    m = score_domain_match(ORG, site(domain=domain, titles=["Acme Plumbing Ltd"], company_numbers=["01234567"], postcodes=["SW1A 1AA"], source_kind="registry"))
    assert m.status == "mismatch" and m.confidence == 0 and m.evidence[0]["type"] == "intermediary_domain"


def test_redirect_to_intermediary_rejected():
    m = score_domain_match(ORG, site(redirected_domain="facebook.com", titles=["Acme"]))
    assert m.status == "mismatch"


@pytest.mark.parametrize("reason", ["domain_for_sale", "parked", "expired", "default_page", "suspended", "parking_host"])
def test_parked_expired_rejected(reason):
    m = score_domain_match(ORG, site(parked=reason, titles=["Acme Plumbing Ltd"], company_numbers=["01234567"]))
    assert m.status == "mismatch" and m.parked


def test_unreachable_is_unknown():
    m = score_domain_match(ORG, site(reachable=False, dns_failure=True))
    assert m.status == "unknown" and m.evidence[0]["type"] == "unreachable"


def test_conflicting_company_number_never_confirmed():
    m = score_domain_match(ORG, site(company_numbers=["99999999"], titles=["Acme Plumbing Ltd"], postcodes=["SW1A 1AA"], phones=["020 7946 0000"]))
    assert m.status in ("review", "mismatch") and any(e["type"] == "company_number_conflict" for e in m.evidence)


def test_reachable_guess_without_any_trace_is_mismatch():
    m = score_domain_match(ORG, site(titles=["Best Pizza in Town"], text_samples=["pizza " * 100], source_kind="guess"))
    assert m.status == "mismatch"
    m2 = score_domain_match(ORG, site(titles=["Best Pizza in Town"], text_samples=["pizza " * 100], source_kind="osm"))
    assert m2.status in ("unknown", "review")


def test_org_without_any_attributes_is_unknown():
    m = score_domain_match(SimpleNamespace(), site(titles=["Whatever"]))
    assert m.status == "unknown"


# ---------------------------------------------------------------- resolve (mock web + PostgreSQL)
async def public(host, port):
    return ["93.184.216.34"]


def web(handlers):
    """handlers: {host: {path: html}}; unknown hosts/paths 404. Records requests."""
    seen = []

    def h(req):
        seen.append(f"{req.url.host}{req.url.path}")
        page = handlers.get(req.url.host, {}).get(req.url.path)
        if req.url.path == "/robots.txt" and page is None:
            return httpx.Response(404)
        if page is None:
            return httpx.Response(404, text="nf", headers={"Content-Type": "text/html"})
        return httpx.Response(200, text=page, headers={"Content-Type": "text/html"})

    return httpx.MockTransport(h), seen


def fetcher_for(transport):
    return SafeFetcher(resolver=public, transport=transport, allow_private_networks=False)


async def make_org(session, **kw):
    vals = dict(legal_name="Acme Plumbing Limited", trading_name="Acme Plumbers", company_number="01234567",
                country_code="GB", postal_code="SW1A 1AA", phone="020 7946 0000", canonical_identity="gb:01234567")
    vals.update(kw)
    o = Organisation(**vals)
    session.add(o)
    await session.flush()
    return o


FOOTER = "<footer>Acme Plumbing Ltd. Registered in England and Wales No. 01234567. 1 High Street, London SW1A 1AA. Tel: 020 7946 0000</footer>"
OPTS = CrawlOptions(max_pages=3, max_depth=1, max_pdfs=0, use_sitemap=False, time_budget_s=10)


async def test_resolve_confirms_by_company_number_and_stores_evidence(session):
    org = await make_org(session)
    transport, seen = web({"acme-plumbing.co.uk": {"/": f"<html><head><title>Acme Plumbing</title></head><body><a href='/contact'>Contact</a>{FOOTER}</body></html>",
                                                   "/contact": "<html><body>info@acme-plumbing.co.uk</body></html>"}})
    f = fetcher_for(transport)
    res = await resolve_organisation_domain(session, f, org.id, ["www.acme-plumbing.co.uk", "acme-plumbing.co.uk", "acmeplumbing.com"],
                                            crawl_options=OPTS, limiter=RateLimiter(min_interval=0))
    await f.aclose()
    await session.commit()
    assert res.best is not None and res.best.verified_match_status == "confirmed"
    rows = (await session.execute(select(OrganisationDomain))).scalars().all()
    assert len(rows) == 1  # www dedupe + stopped at first confirmed
    r = rows[0]
    assert r.domain == "acme-plumbing.co.uk" and r.registrable_domain == "acme-plumbing.co.uk"
    assert r.confidence >= 0.9 and r.website_url == "https://acme-plumbing.co.uk/" and not r.parked
    assert r.last_checked_at and r.last_crawled_at and not r.manual_override
    types = {e["type"] for e in r.evidence}
    assert {"company_number_match", "postcode_match", "phone_match"} <= types
    assert all(e["observed_at"] for e in r.evidence)
    assert "acme-plumbing.co.uk" in res.crawls and {e.email for e in res.crawls["acme-plumbing.co.uk"].emails} == {"info@acme-plumbing.co.uk"}


async def test_resolve_guess_is_review_only(session):
    org = await make_org(session, company_number=None, postal_code=None, phone=None)
    transport, _ = web({"acmeplumbing.co.uk": {"/": "<html><head><title>Acme Plumbing Ltd - boilers</title></head><body><h1>Acme Plumbing</h1></body></html>"}})
    f = fetcher_for(transport)
    cands = candidate_domains_from_name(org.legal_name)
    res = await resolve_organisation_domain(session, f, org.id, cands, crawl_options=OPTS, limiter=RateLimiter(min_interval=0), max_candidates=8)
    await f.aclose()
    row = next(r for r, _ in res.rows if r.domain == "acmeplumbing.co.uk")
    assert row.verified_match_status == "review" and row.confidence <= 0.69
    other = [r for r, _ in res.rows if r.domain != "acmeplumbing.co.uk"]
    assert all(r.verified_match_status in ("unknown", "mismatch") for r in other)  # dead guesses never confirmed


async def test_resolve_parked_and_intermediary(session):
    org = await make_org(session)
    transport, seen = web({"parked-acme.com": {"/": "<html><head><title>parked-acme.com</title></head><body><h1>This domain is for sale</h1></body></html>"}})
    f = fetcher_for(transport)
    res = await resolve_organisation_domain(session, f, org.id, ["facebook.com", "parked-acme.com"], crawl_options=OPTS,
                                            limiter=RateLimiter(min_interval=0))
    await f.aclose()
    by = {r.domain: r for r, _ in res.rows}
    assert by["facebook.com"].verified_match_status == "mismatch" and not any(s.startswith("facebook.com") for s in seen)
    assert by["parked-acme.com"].verified_match_status == "mismatch" and by["parked-acme.com"].parked
    assert res.best is None


async def test_resolve_never_overwrites_manual_override(session):
    org = await make_org(session)
    session.add(OrganisationDomain(organisation_id=org.id, domain="acme-plumbing.co.uk", registrable_domain="acme-plumbing.co.uk",
                                   verified_match_status="confirmed", confidence=1.0, evidence=[{"type": "manual", "detail": "operator", "weight": 1, "observed_at": "x"}],
                                   manual_override=True))
    await session.flush()
    transport, seen = web({"acme-plumbing.co.uk": {"/": "<html><body>Totally different business</body></html>"}})
    f = fetcher_for(transport)
    res = await resolve_organisation_domain(session, f, org.id, ["acme-plumbing.co.uk"], crawl_options=OPTS, limiter=RateLimiter(min_interval=0))
    await f.aclose()
    await session.commit()
    row = (await session.execute(select(OrganisationDomain))).scalar_one()
    assert (row.verified_match_status, row.confidence, row.manual_override) == ("confirmed", 1.0, True)
    assert row.evidence[0]["type"] == "manual" and seen == []  # no crawl either
    assert res.best is row and ("acme-plumbing.co.uk", "manual_override") in res.skipped


async def test_resolve_unreachable_is_unknown_and_missing_org_raises(session):
    org = await make_org(session)

    async def nx(host, port):
        from app.crawler.safe_http import DnsFailure
        raise DnsFailure("nx")

    f = SafeFetcher(resolver=nx, transport=httpx.MockTransport(lambda r: httpx.Response(200)), allow_private_networks=False)
    res = await resolve_organisation_domain(session, f, org.id, ["dead-acme.co.uk"], crawl_options=OPTS, limiter=RateLimiter(min_interval=0))
    assert res.rows[0][0].verified_match_status == "unknown" and res.best is None
    with pytest.raises(ValueError):
        await resolve_organisation_domain(session, f, 99999, ["x.com"])
    await f.aclose()
    assert res.rows[0][0].evidence[0]["type"] == "unreachable"


async def test_resolve_registry_candidate_source_reflected(session):
    org = await make_org(session, company_number=None, phone=None, postal_code="SW1A 1AA")
    transport, _ = web({"acme-plumbing.co.uk": {"/": f"<html><head><title>Acme Plumbing</title></head><body>{FOOTER}</body></html>"}})
    f = fetcher_for(transport)
    res = await resolve_organisation_domain(session, f, org.id, [Candidate("acme-plumbing.co.uk", "registry")], crawl_options=OPTS, limiter=RateLimiter(min_interval=0))
    await f.aclose()
    assert res.best.verified_match_status in ("high", "confirmed")
    assert "registry_reference" in {e["type"] for e in res.best.evidence}
