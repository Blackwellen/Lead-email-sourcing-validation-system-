from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.discovery.completeness import FLAGS, WEIGHTS, _Facts, compute_completeness, evaluate, refresh_completeness
from app.models import (
    Contact,
    ContactEvidence,
    DomainCache,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    SourcingRun,
    ValidationResult,
    WebsiteSnapshot,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


async def mk(session, name="Acme Ltd"):
    o = Organisation(legal_name=name, country_code="GB", canonical_identity=f"GB:nm:{name}")
    session.add(o)
    await session.flush()
    return o


def email(org, addr, **kw):
    local, _, dom = addr.partition("@")
    return EmailAddress(organisation_id=org.id, email_original=addr, email_normalised=addr, local_part=local, domain=dom,
                        registrable_domain=dom, source_type="mailto", **kw)


async def test_nothing_researched_is_all_unknown_never_no(session):
    o = await mk(session)
    c = await compute_completeness(session, o.id, now=NOW)
    assert set(c.flags) == set(FLAGS) and all(v == "unknown" for v in c.flags.values())
    assert c.score == 0 and c.research_progress == 0
    assert {m["classification"] for m in c.missing_opportunities} == {"incomplete_research"}
    assert all(m["next_step"] for m in c.missing_opportunities)
    # a rejected candidate domain alone is NOT yet 'no website': the ladder has not run
    session.add(OrganisationDomain(organisation_id=o.id, domain="wrong.co.uk", registrable_domain="wrong.co.uk",
                                   verified_match_status="mismatch", last_checked_at=NOW))
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["official_website"] == "unknown" and "ladder not finished" in c.reasons["official_website"]
    # ladder exhausted after real domain steps -> genuinely unavailable
    session.add(OrganisationCompleteness(organisation_id=o.id, gap_exhausted_at=NOW,
                                         gap_log=[{"step": "resolve_bulk_candidate_domains", "outcome": "none"}]))
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["official_website"] == "no"
    assert c.flags["published_email"] == "no"  # no official site after the full ladder
    assert [m for m in c.missing_opportunities if m["flag"] == "official_website"][0]["classification"] == "genuinely_unavailable"
    # but an exhausted ladder that never ran a domain step proves nothing
    await session.execute(text("update organisation_completeness set gap_log='[]'::jsonb"))
    await session.execute(text("update organisation_domains set last_checked_at=NULL"))
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["official_website"] == "unknown"


async def test_confirmed_domain_crawl_and_emails(session):
    o = await mk(session)
    d = OrganisationDomain(organisation_id=o.id, domain="acme.co.uk", registrable_domain="acme.co.uk", verified_match_status="confirmed",
                           confidence=0.95, last_checked_at=NOW, last_crawled_at=NOW)
    session.add(d)
    o.last_checked_at = NOW - timedelta(days=3)
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["official_website"] == "yes" and c.flags["recently_checked"] == "yes"
    # crawled, nothing found: researched & unavailable
    assert c.flags["contact_page_inspected"] == "no" and c.flags["published_email"] == "no"
    assert c.flags["email_infrastructure_checked"] == "unknown" and c.flags["campaign_eligibility"] == "unknown"
    # a contact snapshot + an email
    session.add(WebsiteSnapshot(domain_id=d.id, url="https://acme.co.uk/contact", page_kind="contact", content_hash="h"))
    e = email(o, "info@acme.co.uk", origin="published")
    session.add(e)
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["contact_page_inspected"] == "yes" and c.flags["published_email"] == "yes"
    assert c.flags["campaign_eligibility"] == "unknown"
    # infrastructure + recipient + eligibility
    session.add(ValidationResult(email_id=e.id, check_type="V03_mx", result="accepts_mail"))
    e.marketing_status, e.last_validated_at, e.recipient_evidence_status = "eligible", NOW, "smtp_recipient_accepted"
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["email_infrastructure_checked"] == "yes" and c.flags["recipient_evidence"] == "yes" and c.flags["campaign_eligibility"] == "yes"
    # validated but nothing eligible -> researched & unavailable
    e.marketing_status = "needs_review"
    await session.flush()
    assert (await compute_completeness(session, o.id, now=NOW)).flags["campaign_eligibility"] == "no"
    # stale check
    o.last_checked_at = NOW - timedelta(days=90)
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["recently_checked"] == "no"
    assert [m for m in c.missing_opportunities if m["flag"] == "recently_checked"][0]["classification"] == "stale"
    assert 0 < c.score <= 100


async def test_infra_via_domain_cache_and_smtp_attempt(session):
    o = await mk(session)
    e = email(o, "a@x.co.uk", origin="published")
    session.add_all([e, DomainCache(domain="x.co.uk", mail_status="no_mail_service", expires_at=NOW + timedelta(days=1))])
    await session.flush()
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["email_infrastructure_checked"] == "yes" and c.flags["recipient_evidence"] == "unknown"
    session.add(ValidationResult(email_id=e.id, check_type="V07_smtp", result="untested"))
    await session.flush()
    assert (await compute_completeness(session, o.id, now=NOW)).flags["recipient_evidence"] == "unknown"  # 'untested' is not research
    session.add(ValidationResult(email_id=e.id, check_type="V07_smtp", result="inconclusive"))
    await session.flush()
    assert (await compute_completeness(session, o.id, now=NOW)).flags["recipient_evidence"] == "no"


async def test_in_progress_from_queued_jobs(session):
    o = await mk(session)
    run = SourcingRun(name="r", status="running")
    session.add(run)
    await session.flush()
    await session.execute(text(
        "INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, state) VALUES (:r,'resolve_domain','k',CAST(:p AS jsonb),'queued')"),
        {"r": run.id, "p": f'{{"organisation_id": {o.id}}}'})
    c = await compute_completeness(session, o.id, now=NOW)
    assert c.flags["official_website"] == "in_progress" and c.flags["published_email"] == "in_progress"
    assert all(m["classification"] == "in_progress" for m in c.missing_opportunities if m["flag"] in ("official_website", "published_email"))
    # candidate awaiting human review is also in progress
    o2 = await mk(session, "Rev Ltd")
    session.add(OrganisationDomain(organisation_id=o2.id, domain="rev.co.uk", registrable_domain="rev.co.uk", verified_match_status="review"))
    await session.flush()
    assert (await compute_completeness(session, o2.id, now=NOW)).flags["official_website"] == "in_progress"


async def test_person_corroboration(session):
    o = await mk(session)
    d = OrganisationDomain(organisation_id=o.id, domain="acme.co.uk", registrable_domain="acme.co.uk", verified_match_status="confirmed",
                           last_crawled_at=NOW)
    c1 = Contact(organisation_id=o.id, full_name="Jane Doe", identity_key="name:jane doe")
    session.add_all([d, c1])
    await session.flush()
    e = email(o, "jane@acme.co.uk", origin="published", contact_id=c1.id)
    session.add(e)
    await session.flush()
    r = await compute_completeness(session, o.id, now=NOW)
    assert r.flags["contact_person_corroborated"] == "in_progress"
    ev1, ev2 = EvidenceSource(source_adapter="a", source_url="https://acme.co.uk/team", evidence_type="mailto"), \
        EvidenceSource(source_adapter="b", source_url="https://dir.example/x", evidence_type="text")
    session.add_all([ev1, ev2])
    await session.flush()
    session.add_all([ContactEvidence(email_id=e.id, contact_id=c1.id, organisation_id=o.id, evidence_id=ev1.id, extraction_method="mailto"),
                     ContactEvidence(email_id=e.id, contact_id=c1.id, organisation_id=o.id, evidence_id=ev2.id, extraction_method="text")])
    await session.flush()
    r = await compute_completeness(session, o.id, now=NOW)
    assert r.flags["contact_person_corroborated"] == "yes" and r.flags["contact_page_inspected"] == "yes"  # evidence url has /team


async def test_refresh_bulk_upsert_preserves_gap_memory(session):
    orgs = [await mk(session, f"O{i} Ltd") for i in range(5)]
    session.add(OrganisationCompleteness(organisation_id=orgs[0].id, gap_steps_tried=["x"], gap_log=[{"step": "x"}], needs_review=True))
    session.add(OrganisationDomain(organisation_id=orgs[1].id, domain="o1.co.uk", registrable_domain="o1.co.uk", verified_match_status="confirmed"))
    await session.flush()
    res = await refresh_completeness(session, [o.id for o in orgs] + [orgs[0].id], now=NOW)
    assert len(res) == 5 and res[orgs[1].id].flags["official_website"] == "yes" and res[orgs[1].id].score == WEIGHTS["official_website"]
    rows = {r.organisation_id: r for r in (await session.execute(select(OrganisationCompleteness))).scalars()}
    assert len(rows) == 5 and rows[orgs[1].id].official_website == "yes" and rows[orgs[1].id].score == 25
    assert rows[orgs[0].id].gap_steps_tried == ["x"] and rows[orgs[0].id].needs_review is True
    assert res[orgs[0].id].needs_review is True


def test_truthfulness_invariants_exhaustive():
    """For every combination of research facts, a 'no' flag requires the corresponding research to have happened."""
    names = ["crawled", "snap", "emails", "jobs_domain", "exhausted", "domain_step", "rejected", "smtp", "named"]
    for combo in itertools.product([False, True], repeat=len(names)):
        k = dict(zip(names, combo, strict=True))
        f = _Facts(1)
        if k["crawled"] or k["rejected"]:
            f.domains.append({"status": "mismatch" if k["rejected"] else "unknown", "parked": False, "manual_override": False,
                              "last_checked_at": NOW if k["rejected"] else None, "last_crawled_at": NOW if k["crawled"] else None})
        f.snapshot_kinds = {"contact"} if k["snap"] else set()
        f.emails_total = f.emails_published = 1 if k["emails"] else 0
        f.jobs_domain, f.gap_exhausted, f.smtp_attempted = k["jobs_domain"], k["exhausted"], k["smtp"]
        f.named_with_email = 1 if k["named"] else 0
        f.gap_log = [{"step": "search_provider", "outcome": "none"}] if k["domain_step"] else []
        c = evaluate(f, now=NOW)
        fl = c.flags
        if fl["official_website"] == "no":
            assert k["exhausted"] and (k["rejected"] or k["domain_step"]) and not k["jobs_domain"]
        if fl["contact_page_inspected"] == "no":
            assert k["crawled"] and not k["snap"]
        if fl["published_email"] == "no":
            assert not k["emails"] and (k["crawled"] or fl["official_website"] == "no")
        if fl["recipient_evidence"] == "no":
            assert k["smtp"]
        if fl["campaign_eligibility"] == "no":
            assert False, "no addresses validated in this sweep"
        if fl["recently_checked"] in ("yes", "no"):
            assert False, "never checked in this sweep"
        if fl["contact_person_corroborated"] == "no":
            assert k["crawled"] or k["named"]
        assert 0 <= c.score <= 100 and sum(WEIGHTS.values()) == 100
