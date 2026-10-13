from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.discovery.gap_recovery import (
    DEFAULT_STEPS,
    STEP_ORDER,
    GapBudget,
    GapDeps,
    StepResult,
    YieldTracker,
    run_gap_recovery,
)
from app.models import (
    ContactEvidence,
    DocumentRecord,
    EmailAddress,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    OrganisationSource,
    SourcingRun,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)
ALL = {"brave_search", "commoncrawl_index"}


async def mk(session, name="Acme Ltd", **kw):
    o = Organisation(legal_name=name, country_code="GB", canonical_identity=f"GB:nm:{name}", **kw)
    session.add(o)
    await session.flush()
    return o


def recorder(calls, results=None, side=None):
    """Injectable step implementations that record calls and return scripted results."""
    results = results or {}

    def make(name):
        async def impl(ctx):
            calls.append(name)
            if side and name in side:
                await side[name](ctx)
            return results.get(name, StepResult("none", requests=2))
        return impl

    return {n: make(n) for n in STEP_ORDER}


async def give_website(ctx):
    ctx.session.add(OrganisationDomain(organisation_id=ctx.org.id, domain="acme.co.uk", registrable_domain="acme.co.uk",
                                       verified_match_status="confirmed", confidence=0.9))
    await ctx.session.flush()


async def give_email(ctx):
    ctx.session.add(EmailAddress(organisation_id=ctx.org.id, email_original="info@acme.co.uk", email_normalised="info@acme.co.uk",
                                 local_part="info", domain="acme.co.uk", registrable_domain="acme.co.uk", source_type="mailto", origin="published"))
    await ctx.session.flush()


async def test_ordered_ladder_logged_and_not_repeated(session):
    o = await mk(session)
    calls: list[str] = []
    r = await run_gap_recovery(session, o.id, run_id=None, policy="try_other_sources", steps=recorder(calls), available_adapters=ALL, now=NOW)
    # role candidates need allow_inferred + a confirmed domain -> skipped; everything else ran in ladder order
    assert calls == list(STEP_ORDER[:-1]) and r.stop_reason == "ladder_exhausted" and r.exhausted
    assert [s["step"] for s in r.skipped] == ["role_address_candidates"]
    row = (await session.execute(select(OrganisationCompleteness))).scalar_one()
    assert row.gap_steps_tried == list(STEP_ORDER[:-1]) and row.gap_exhausted_at is not None
    assert [e["step"] for e in row.gap_log] == list(STEP_ORDER[:-1]) and all(e["at"] and e["outcome"] == "none" for e in row.gap_log)
    assert row.gap_log[0]["yield"] == {"website": 0, "emails": 0, "domains": 0, "speculative": 0}
    # exhausted ladder with real steps -> now (and only now) the answer is 'no'
    assert r.completeness.flags["official_website"] == "no" and row.official_website == "no"
    # second run inside the TTL repeats nothing
    calls.clear()
    r2 = await run_gap_recovery(session, o.id, run_id=None, policy="try_other_sources", steps=recorder(calls), available_adapters=ALL, now=NOW + timedelta(days=5))
    assert calls == [] and {s["reason"] for s in r2.skipped} >= {"ttl"} and r2.exhausted
    # after the TTL the steps may run again
    r3 = await run_gap_recovery(session, o.id, run_id=None, policy="try_other_sources", steps=recorder(calls), available_adapters=ALL, now=NOW + timedelta(days=31))
    assert len(calls) == 6 and r3.ran


async def test_stops_when_website_and_email_found(session):
    o = await mk(session)
    calls: list[str] = []
    steps = recorder(calls, {"resolve_bulk_candidate_domains": StepResult("found", website=1, emails=1, requests=3)},
                     side={"resolve_bulk_candidate_domains": lambda ctx: _both(ctx)})
    r = await run_gap_recovery(session, o.id, run_id=None, policy="try_other_sources", steps=steps, available_adapters=ALL, now=NOW)
    assert calls == ["recheck_imported_website", "resolve_bulk_candidate_domains"]
    assert r.complete and r.stop_reason == "website_and_email_found" and not r.exhausted
    assert r.completeness.flags["official_website"] == "yes" and r.completeness.flags["published_email"] == "yes"
    assert r.requests_used == 5


async def _both(ctx):
    await give_website(ctx)
    await give_email(ctx)


async def test_request_budget_is_a_hard_cap_and_does_not_prove_absence(session):
    o = await mk(session)
    calls: list[str] = []
    steps = recorder(calls, {n: StepResult("none", requests=3) for n in STEP_ORDER})
    r = await run_gap_recovery(session, o.id, run_id=None, budget=GapBudget(max_requests=7), policy="try_other_sources", steps=steps,
                               available_adapters=ALL, now=NOW)
    assert calls == ["recheck_imported_website", "resolve_bulk_candidate_domains", "search_provider"]  # 3+3+3 >= 7 -> stop before the 4th
    assert r.stop_reason == "request_budget_exhausted" and r.requests_used == 9 and not r.exhausted
    assert r.completeness.flags["official_website"] == "unknown"  # budget ran out: NOT "no"
    assert (await session.execute(select(OrganisationCompleteness.gap_exhausted_at))).scalar_one() is None
    assert (await run_gap_recovery(session, o.id, run_id=None, budget=5, steps=recorder([]), now=NOW + timedelta(days=40),
                                   policy="try_other_sources", available_adapters=ALL)).requests_used <= 5 + 2 * 7


async def test_search_budget_and_adapter_availability(session):
    o = await mk(session)
    calls: list[str] = []
    r = await run_gap_recovery(session, o.id, run_id=None, budget=GapBudget(max_search=0), policy="try_other_sources", steps=recorder(calls),
                               available_adapters=ALL, now=NOW)
    assert "search_provider" not in calls and {"step": "search_provider", "reason": "search_budget"} in r.skipped
    o2 = await mk(session, "Other Ltd")
    calls.clear()
    r2 = await run_gap_recovery(session, o2.id, run_id=None, policy="try_other_sources", steps=None, available_adapters=set(), now=NOW)
    reasons = {s["step"]: s["reason"] for s in r2.skipped}
    assert reasons["search_provider"] == "adapter_unavailable" and reasons["common_crawl_lookup"] == "adapter_unavailable"
    # searches are counted against the per-company search budget
    o3 = await mk(session, "Third Ltd")
    steps = recorder([], {"search_provider": StepResult("none", searches=1, requests=1)})
    r3 = await run_gap_recovery(session, o3.id, run_id=None, budget=GapBudget(max_search=1), policy="try_other_sources", steps=steps,
                                available_adapters=ALL, now=NOW)
    assert r3.searches_used == 1


async def test_marginal_yield_skips_poor_step_types(session):
    o = await mk(session)
    t = YieldTracker()
    for _ in range(25):
        t.record("search_provider", False)
    for i in range(25):
        t.record("common_crawl_lookup", i % 2 == 0)  # 52% success: kept
    t.record("recheck_imported_website", False)  # below the minimum sample: still allowed
    calls: list[str] = []
    r = await run_gap_recovery(session, o.id, run_id=None, tracker=t, min_sample=20, min_success_rate=0.1, policy="try_other_sources",
                               steps=recorder(calls, {"register_corroboration": StepResult("found", domains=1, requests=1)}), available_adapters=ALL, now=NOW)
    assert "search_provider" not in calls and "common_crawl_lookup" in calls and "recheck_imported_website" in calls
    assert {"step": "search_provider", "reason": "low_yield"} in r.skipped
    assert t.attempts("register_corroboration") == 1 and t.rate("register_corroboration") == 1.0
    # tracker survives in the run counters
    run = SourcingRun(name="r", status="running")
    session.add(run)
    await session.flush()
    await t.save(session, run.id)
    await session.refresh(run)
    loaded = await YieldTracker.load(session, run.id)
    assert loaded.rate("search_provider") == 0.0 and loaded.attempts("common_crawl_lookup") == 26  # 25 + the attempt made in this run


async def test_policy_modes(session):
    # stop_if_no_website: only the two website steps run
    o = await mk(session)
    calls: list[str] = []
    r = await run_gap_recovery(session, o.id, run_id=None, policy="stop_if_no_website", steps=recorder(calls), available_adapters=ALL, now=NOW)
    assert calls == list(STEP_ORDER[:2]) and r.stop_reason == "no_website_policy" and r.exhausted and not r.needs_review
    # try_other_sources: whole ladder
    o2 = await mk(session, "B Ltd")
    calls2: list[str] = []
    r2 = await run_gap_recovery(session, o2.id, run_id=None, policy="try_other_sources", steps=recorder(calls2), available_adapters=ALL, now=NOW)
    assert len(calls2) == 6 and not r2.needs_review
    # flag_for_review: whole ladder, then the organisation is flagged
    o3 = await mk(session, "C Ltd")
    calls3: list[str] = []
    r3 = await run_gap_recovery(session, o3.id, run_id=None, policy="flag_for_review", steps=recorder(calls3), available_adapters=ALL, now=NOW)
    assert len(calls3) == 6 and r3.needs_review and r3.exhausted
    assert (await session.get(OrganisationCompleteness, o3.id)).needs_review is True
    # flagged only if still no website: a found site is never flagged
    o4 = await mk(session, "D Ltd")
    r4 = await run_gap_recovery(session, o4.id, run_id=None, policy="flag_for_review", available_adapters=ALL, now=NOW,
                                steps=recorder([], {"recheck_imported_website": StepResult("found", website=1)}, side={"recheck_imported_website": give_website}))
    assert not r4.needs_review and r4.website_found
    with pytest.raises(ValueError):
        await run_gap_recovery(session, o.id, run_id=None, policy="bogus")


async def test_step_errors_do_not_abort_the_ladder(session):
    o = await mk(session)
    calls: list[str] = []

    async def boom(ctx):
        calls.append("boom")
        raise RuntimeError("provider down")

    steps = recorder(calls)
    steps["recheck_imported_website"] = boom
    r = await run_gap_recovery(session, o.id, run_id=None, policy="try_other_sources", steps=steps, available_adapters=ALL, now=NOW)
    assert calls[0] == "boom" and len(calls) == 6
    assert r.ran[0]["outcome"] == "error"
    row = (await session.execute(select(OrganisationCompleteness))).scalar_one()
    assert "recheck_imported_website" not in row.gap_steps_tried  # an error is not a performed research step
    assert any(e["outcome"] == "error" for e in row.gap_log)


async def test_default_role_candidates_flagged_speculative_and_limited(session):
    o = await mk(session, industry="property lettings")
    session.add(OrganisationDomain(organisation_id=o.id, domain="acme.co.uk", registrable_domain="acme.co.uk", verified_match_status="confirmed", confidence=0.9))
    await session.flush()
    # not allowed unless the run allows inferred contacts
    r = await run_gap_recovery(session, o.id, run_id=None, ladder=["role_address_candidates"], allow_inferred=False, now=NOW)
    assert r.skipped == [{"step": "role_address_candidates", "reason": "inferred_not_allowed"}]
    assert not (await session.execute(select(EmailAddress))).scalars().all()
    r = await run_gap_recovery(session, o.id, run_id=None, ladder=["role_address_candidates"], allow_inferred=True,
                               budget=GapBudget(role_candidates=2), now=NOW)
    ems = (await session.execute(select(EmailAddress))).scalars().all()
    assert len(ems) == 2 and all(e.origin == "role_speculative" and e.needs_review and e.domain == "acme.co.uk" and e.source_type == "permutation" for e in ems)
    assert r.ran[0]["speculative"] == 2 and not r.email_found  # speculative addresses never count as "email found"
    ev = (await session.execute(select(ContactEvidence).where(ContactEvidence.extraction_method == "permutation"))).scalars().all()
    assert len(ev) == 2 and ev[0].context["origin"] == "role_speculative"
    # hard cap
    o2 = await mk(session, "Big Ltd")
    session.add(OrganisationDomain(organisation_id=o2.id, domain="big.co.uk", registrable_domain="big.co.uk", verified_match_status="high", confidence=0.9))
    await session.flush()
    await run_gap_recovery(session, o2.id, run_id=None, ladder=["role_address_candidates"], allow_inferred=True, budget=GapBudget(role_candidates=999), now=NOW)
    n = len((await session.execute(select(EmailAddress).where(EmailAddress.organisation_id == o2.id))).scalars().all())
    assert 0 < n <= 10


async def test_default_document_extraction_step(session):
    o = await mk(session)
    session.add(DocumentRecord(source_adapter="doc", source_url="https://acme.co.uk/brochure.pdf", file_sha256="a" * 64, doc_type="pdf",
                               locator="page 1, line 2", organisation_id=o.id,
                               extracted={"fields": {"emails": ["sales@acme.co.uk"], "name": "Pat Lee", "role": "Sales"}}))
    await session.flush()
    r = await run_gap_recovery(session, o.id, run_id=None, ladder=["document_extraction"], policy="try_other_sources", now=NOW)
    assert r.ran[0]["emails"] == 1
    e = (await session.execute(select(EmailAddress))).scalar_one()
    assert e.email_normalised == "sales@acme.co.uk" and e.organisation_id == o.id
    again = await run_gap_recovery(session, o.id, run_id=None, ladder=["document_extraction"], policy="try_other_sources", now=NOW + timedelta(days=40))
    assert again.ran[0]["outcome"] == "none"  # already stored: nothing new


async def test_default_steps_use_source_fields_and_injected_resolver(session):
    o = await mk(session)
    session.add(OrganisationSource(organisation_id=o.id, adapter_id="osm", source_ref="1", website="https://www.acme-site.co.uk/",
                                   email_published="hello@acme-bulk.co.uk", raw={"website": "https://www.acme-site.co.uk", "domain": "facebook.com"}))
    await session.flush()
    seen: list[list[str]] = []

    async def fake_resolve(ctx, cands):
        seen.append([c.domain for c in cands])
        ctx.tried_domains.update(c.domain for c in cands)
        return StepResult("none", domains=len(cands), requests=4)

    deps = GapDeps(resolve_fn=fake_resolve)
    r = await run_gap_recovery(session, o.id, run_id=None, ladder=["recheck_imported_website", "resolve_bulk_candidate_domains"],
                               deps=deps, policy="try_other_sources", now=NOW)
    # step 1: the dataset's website; step 2: only domains from OTHER bulk fields (free/social/intermediary domains excluded)
    assert seen == [["acme-site.co.uk"], ["acme-bulk.co.uk"]]
    assert r.requests_used == 8 and r.exhausted
    # without a fetcher the network steps are 'unavailable' (and so prove nothing)
    o2 = await mk(session, "NoFetch Ltd")
    session.add(OrganisationSource(organisation_id=o2.id, adapter_id="osm", source_ref="2", website="https://nf.co.uk"))
    await session.flush()
    r2 = await run_gap_recovery(session, o2.id, run_id=None, ladder=["recheck_imported_website"], now=NOW)
    assert r2.skipped and r2.skipped[0]["reason"].startswith("unavailable") and r2.completeness.flags["official_website"] == "unknown"
    assert DEFAULT_STEPS.keys() == set(STEP_ORDER)
