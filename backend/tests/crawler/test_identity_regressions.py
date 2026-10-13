"""Regressions: guessed domains keep their provenance on re-checks; re-checks never downgrade on transient results or wipe evidence."""
import httpx
from sqlalchemy import select

from app.crawler.ratelimit import RateLimiter
from app.crawler.safe_http import SafeFetcher
from app.discovery.gap_recovery import (
    GapBudget,
    GapDeps,
    StepContext,
    StepResult,
    step_recheck_imported_website,
)
from app.models import OrganisationDomain, OrganisationSource
from app.pipeline.identity import (
    Candidate,
    candidate_domains_from_name,
    resolve_organisation_domain,
    stored_candidate_source,
    stored_candidates,
)

from .test_identity import FOOTER, OPTS, fetcher_for, make_org, web

# name in title / h1 plus a self-referencing schema.org url: no company number / postcode / phone ("hard" identity evidence)
TITLE_ONLY = ("<html><head><title>Acme Plumbing Ltd - boilers</title><script type='application/ld+json'>"
              '{"@context":"https://schema.org","@type":"Organization","name":"Acme Plumbing Ltd","url":"https://acmeplumbing.co.uk/"}'
              "</script></head><body><h1>Acme Plumbing</h1></body></html>")


async def _resolve(session, org, cands, pages):
    transport, _ = web(pages)
    f = fetcher_for(transport)
    try:
        return await resolve_organisation_domain(session, f, org.id, cands, crawl_options=OPTS, limiter=RateLimiter(min_interval=0), max_candidates=8)
    finally:
        await f.aclose()


async def test_guessed_domain_is_never_promoted_by_a_recheck(session):
    """Finding 2: a domain created by name guessing is re-scored as a guess (stays 'review'), not as a 'dataset' candidate."""
    org = await make_org(session, company_number=None, postal_code=None, phone=None)
    pages = {"acmeplumbing.co.uk": {"/": TITLE_ONLY}}
    await _resolve(session, org, candidate_domains_from_name(org.legal_name), pages)
    row = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.domain == "acmeplumbing.co.uk"))).scalar_one()
    assert row.verified_match_status == "review" and stored_candidate_source(row.evidence) == "guess"

    # the resolve_domain handler re-checks stored rows with their RECORDED provenance (previously: blanket 'dataset' -> 'high')
    cands = await stored_candidates(session, [row], org.id)
    assert [c.source for c in cands] == ["guess"]
    for _ in range(2):
        await _resolve(session, org, cands, pages)
    again = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.id == row.id)
                                   .execution_options(populate_existing=True))).scalar_one()
    assert again.verified_match_status == "review" and again.confidence <= 0.69
    assert stored_candidate_source(again.evidence) == "guess"

    # control: the same page evidence for a GENUINE dataset candidate is promoted (so the guard above is what keeps the guess down)
    org2 = await make_org(session, company_number=None, postal_code=None, phone=None, canonical_identity="gb:x2")
    res = await _resolve(session, org2, [Candidate("acmeplumbing.co.uk", "dataset")], pages)
    assert res.rows[0][0].verified_match_status == "high"


async def test_gap_recheck_uses_stored_provenance(session):
    """Finding 2 (gap recovery): step_recheck_imported_website passes stored rows with their recorded source; only the
    organisation's own dataset websites are 'dataset'."""
    org = await make_org(session, company_number=None, postal_code=None, phone=None)
    await _resolve(session, org, candidate_domains_from_name(org.legal_name), {"acmeplumbing.co.uk": {"/": TITLE_ONLY}})
    session.add(OrganisationSource(organisation_id=org.id, adapter_id="csv_import", source_ref="r1", website="https://www.acme-official.co.uk/"))
    await session.flush()
    seen: list[Candidate] = []

    async def capture(ctx, cands):
        seen.extend(cands)
        return StepResult("none")

    await step_recheck_imported_website(StepContext(session, org, None, GapBudget(), GapDeps(resolve_fn=capture)))
    by = {c.domain: c.source for c in seen}
    assert by["acme-official.co.uk"] == "dataset"
    assert by.get("acmeplumbing.co.uk") == "guess"


async def test_dataset_source_record_gives_dataset_provenance(session):
    org = await make_org(session, company_number=None, postal_code=None, phone=None)
    session.add(OrganisationSource(organisation_id=org.id, adapter_id="csv_import", source_ref="r1", website="acmeplumbing.co.uk"))
    session.add(OrganisationDomain(organisation_id=org.id, domain="acmeplumbing.co.uk", registrable_domain="acmeplumbing.co.uk",
                                   verified_match_status="unknown", confidence=0.0, evidence=[]))
    await session.flush()
    rows = (await session.execute(select(OrganisationDomain).where(OrganisationDomain.organisation_id == org.id))).scalars().all()
    assert [c.source for c in await stored_candidates(session, rows, org.id)] == ["dataset"]


async def test_unreachable_recheck_never_downgrades_or_wipes_evidence(session):
    """Finding 6: a confirmed row stays confirmed when a re-check cannot reach the site; operator / provenance evidence survives;
    only positive mismatch evidence (parked) lowers the status."""
    org = await make_org(session)
    pages = {"acme-plumbing.co.uk": {"/": f"<html><head><title>Acme Plumbing</title></head><body>{FOOTER}</body></html>"}}
    res = await _resolve(session, org, [Candidate("acme-plumbing.co.uk", "dataset")], pages)
    row = res.best
    assert row.verified_match_status == "confirmed"
    row.evidence = [*row.evidence, {"type": "operator_supplied", "detail": "domain supplied by operator import", "weight": 0.8}]
    await session.flush()

    async def nx(host, port):
        from app.crawler.safe_http import DnsFailure
        raise DnsFailure("nx")

    f = SafeFetcher(resolver=nx, transport=httpx.MockTransport(lambda r: httpx.Response(200)), allow_private_networks=False)
    res2 = await resolve_organisation_domain(session, f, org.id, ["acme-plumbing.co.uk"], crawl_options=OPTS, limiter=RateLimiter(min_interval=0))
    await f.aclose()
    row = res2.rows[0][0]
    types = {e["type"] for e in row.evidence}
    assert row.verified_match_status == "confirmed" and row.confidence >= 0.9
    assert {"operator_supplied", "company_number_match", "unreachable", "candidate_source"} <= types
    assert stored_candidate_source(row.evidence) == "dataset"
    assert len(row.evidence) <= 40

    # positive counter-evidence does downgrade
    res3 = await _resolve(session, org, ["acme-plumbing.co.uk"],
                          {"acme-plumbing.co.uk": {"/": "<html><head><title>acme-plumbing.co.uk</title></head><body><h1>This domain is for sale</h1></body></html>"}})
    assert res3.rows[0][0].verified_match_status == "mismatch" and res3.rows[0][0].parked
