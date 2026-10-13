"""Regressions: operator marketing policy is applied, 'eligible' is gated, passive catch-all suspicion is shared per domain."""
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.config import Settings
from app.exports import E, ExportFilters, _from, filter_conditions
from app.models import ContactEvidence, EmailAddress, EvidenceSource
from app.settings_schema import CountryMarketingPolicy, ValidationSettings, save_section
from app.verify import pipeline as vp
from app.verify.marketing import marketing_status
from app.verify.pipeline import verify_email

from .fakes import FakeResolver
from .test_pipeline import good_dns, make_email

ELIGIBLE_GB = {"GB": CountryMarketingPolicy(personal="eligible", role="eligible", allow_eligible=True)}


async def _set_policy(session, policy):
    await save_section(session, "validation", ValidationSettings(marketing_policy=policy))
    await session.commit()
    vp.invalidate_marketing_policy_cache()


async def test_operator_marketing_policy_is_applied_by_verification(session):
    """Finding 11: the admin 'validation' policy (DB setting) reaches verify_email (env Settings carry no policy)."""
    e = await make_email(session)
    out = await verify_email(session, e.id, mode="fast", resolver=good_dns(), settings=Settings())
    assert out.marketing_status == "needs_review"  # built-in default never 'eligible'
    try:
        await _set_policy(session, ELIGIBLE_GB)
        out = await verify_email(session, e.id, mode="fast", resolver=good_dns(), settings=Settings())
        assert out.marketing_status == "eligible", out.marketing_reason
    finally:
        await _set_policy(session, {})


async def _add_evidence(session, e, *, historical=False, etype="mailto", tag="x"):
    ev = EvidenceSource(source_adapter="commoncrawl" if historical else "permutation_research", source_url=f"https://{e.domain}/{tag}",
                        evidence_type=etype, source_reliability=0.1, content_hash=tag + e.local_part, historical=historical,
                        observed_at=datetime.now(UTC))
    session.add(ev)
    await session.flush()
    session.add(ContactEvidence(email_id=e.id, evidence_id=ev.id, extraction_method=etype, organisation_id=e.organisation_id))
    await session.commit()


async def test_eligible_is_gated_on_live_published_evidence(session):
    """Finding 12: even with an 'eligible' operator policy, archive-only / guessed / bounced addresses are never eligible."""
    archived = await make_email(session, local="old.staff", published=False)
    await _add_evidence(session, archived, historical=True, etype="mailto", tag="arch")
    guessed = await make_email(session, local="info", published=False, origin="role_speculative")
    await _add_evidence(session, guessed, etype="permutation", tag="perm")
    bounced = await make_email(session, local="gone")
    bounced.previously_bounced = True
    await session.commit()
    good = await make_email(session, local="jane.live")
    try:
        await _set_policy(session, ELIGIBLE_GB)
        res = {}
        for e in (archived, guessed, bounced, good):
            res[e.local_part] = (await verify_email(session, e.id, mode="fast", resolver=good_dns(), settings=Settings())).marketing_status
        assert res == {"old.staff": "needs_review", "info": "needs_review", "gone": "needs_review", "jane.live": "eligible"}, res
    finally:
        await _set_policy(session, {})
    # unit level: the gate also applies to explicit callers
    from types import SimpleNamespace

    pol = SimpleNamespace(marketing_policy={"GB": {"personal": "eligible", "allow_eligible": True}})
    org = SimpleNamespace(id=1, country_code="GB", legal_type="Private limited company", company_number="1", canonical_identity="x", legal_name="A", trading_name=None)
    row = SimpleNamespace(email_normalised="a@b.co.uk", domain="b.co.uk", registrable_domain="b.co.uk", role_type=None, is_free_provider=False,
                          origin="published", overall_reachability="safe", previously_bounced=False, is_disposable=False)
    assert marketing_status(row, org, [], pol, live_evidence=True) == "eligible"
    assert marketing_status(row, org, [], pol, live_evidence=False) == "needs_review"
    assert marketing_status(SimpleNamespace(**{**row.__dict__, "is_disposable": True}), org, [], pol, live_evidence=True) == "needs_review"
    assert marketing_status(SimpleNamespace(**{**row.__dict__, "overall_reachability": "invalid"}), org, [], pol, live_evidence=True) == "needs_review"


async def test_marketing_export_excludes_archive_only_even_if_marked_eligible(session):
    """Finding 12 (exports): listmonk / marketing_eligible_only never exports archive-only or speculative addresses."""
    archived = await make_email(session, local="old.staff", published=False)
    await _add_evidence(session, archived, historical=True, tag="arch")
    live = await make_email(session, local="jane.live")
    await session.execute(text("UPDATE email_addresses SET marketing_status='eligible', overall_reachability='safe'"))
    await session.commit()
    conds = await filter_conditions(session, ExportFilters(marketing_eligible_only=True))
    ids = {r[0] for r in (await session.execute(select(E.id).select_from(_from()).where(*conds))).all()}
    assert ids == {live.id}


async def test_passive_catch_all_suspicion_is_shared_by_the_domain(session):
    """Finding 18: a fresh passive 'suspected' catch-all verdict for A applies to B on the same domain (B must not be 'safe')."""
    dom = "gw-corp.co.uk"
    a = await make_email(session, local="anna", domain=dom)
    b = await make_email(session, local="bob", domain=dom)
    r = FakeResolver().mail_domain(dom, [(10, "mxa-001.pphosted.com")])
    out_a = await verify_email(session, a.id, mode="deep", resolver=r, settings=Settings())
    assert out_a.catch_all_status == "suspected"
    out_b = await verify_email(session, b.id, mode="fast", resolver=r, settings=Settings())
    assert out_b.catch_all_status == "suspected" and out_b.overall_reachability != "safe"
    row = (await session.execute(select(EmailAddress).where(EmailAddress.id == b.id))).scalar_one()
    assert row.catch_all_status == "suspected"
