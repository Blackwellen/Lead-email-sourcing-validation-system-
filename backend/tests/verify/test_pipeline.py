from datetime import UTC, datetime

from sqlalchemy import func, select

from app.config import Settings
from app.models import (
    Contact,
    ContactEvidence,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationDomain,
    SuppressionEntry,
    ValidationResult,
)
from app.verify.delivery import import_delivery_events
from app.verify.pipeline import VERIFIER_VERSION, VerifyOptions, estimate_cost, verify_email
from app.verify.smtp import SmtpProbe

from .conftest import OWNED
from .fakes import FakeResolver, timeout_exc

DOM = "acme-ltd.co.uk"


async def make_email(session, local="jane.smith", domain=DOM, origin="published", published=True, country="GB", legal_type="Private limited company"):
    org = Organisation(legal_name="Acme Ltd", country_code=country, legal_type=legal_type, company_number="01234567",
                       canonical_identity=f"gb:{domain}:{local}")
    session.add(org)
    await session.flush()
    c = Contact(organisation_id=org.id, full_name="Jane Smith", identity_confidence=0.8, identity_key="jane smith")
    session.add(c)
    session.add(OrganisationDomain(organisation_id=org.id, domain=domain, registrable_domain=domain, verified_match_status="confirmed", confidence=0.95))
    await session.flush()
    addr = f"{local}@{domain}"
    e = EmailAddress(organisation_id=org.id, contact_id=c.id, email_original=addr, email_normalised=addr, local_part=local, domain=domain,
                     registrable_domain=domain, source_type="website", origin=origin)
    session.add(e)
    await session.flush()
    if published:
        ev = EvidenceSource(source_adapter="site_crawler", source_url=f"https://www.{domain}/contact", evidence_type="mailto",
                            source_reliability=0.9, content_hash="h" + local, observed_at=datetime.now(UTC))
        session.add(ev)
        await session.flush()
        session.add(ContactEvidence(email_id=e.id, evidence_id=ev.id, extraction_method="mailto", organisation_id=org.id))
    await session.commit()
    return e


def good_dns(domain=DOM):
    return FakeResolver().mail_domain(domain, [(10, f"mx.{domain}")])


async def count_rows(session, email_id, check=None):
    q = select(func.count()).select_from(ValidationResult).where(ValidationResult.email_id == email_id)
    if check:
        q = q.where(ValidationResult.check_type == check)
    return (await session.execute(q)).scalar_one()


async def test_fast_published_is_safe_but_not_a_delivery_claim(session):
    e = await make_email(session)
    out = await verify_email(session, e.id, mode="fast", resolver=good_dns())
    await session.refresh(e)
    assert out.syntax_status == "valid" and out.domain_mail_status == "accepts_mail"
    assert out.recipient_evidence_status == "untested" and out.catch_all_status == "untested"
    assert out.overall_reachability == "safe" and out.confidence >= 70
    assert e.overall_reachability == "safe" and e.confidence == out.confidence and e.last_validated_at is not None
    assert e.marketing_status == "needs_review"
    assert "disclaimer" in e.confidence_breakdown and "not a guarantee" in e.confidence_breakdown["disclaimer"]
    types = {r.check_type for r in (await session.execute(select(ValidationResult).where(ValidationResult.email_id == e.id))).scalars()}
    assert {"V01_syntax", "V02_domain", "V03_mx", "V05_flags"} <= types
    assert all(r.verifier_version == VERIFIER_VERSION and r.expires_at for r in (await session.execute(select(ValidationResult))).scalars())


async def test_idempotent_rerun_no_duplicate_rows_no_new_dns(session):
    e = await make_email(session)
    r = good_dns()
    await verify_email(session, e.id, resolver=r)
    n, calls = await count_rows(session, e.id), len(r.calls)
    out = await verify_email(session, e.id, resolver=r)
    assert await count_rows(session, e.id) == n and len(r.calls) == calls
    assert out.network_skipped is True


async def test_speculative_never_safe_even_with_mx(session):
    e = await make_email(session, local="john.smith", origin="pattern_inferred", published=False)
    out = await verify_email(session, e.id, resolver=good_dns())
    assert out.overall_reachability in ("unknown", "risky") and out.confidence <= 55


async def test_no_mx_null_mx_and_nxdomain_invalid(session):
    e1 = await make_email(session, local="a", domain="nomail-corp.co.uk")
    r = FakeResolver().add("nomail-corp.co.uk", "MX", ["0 ."])
    assert (await verify_email(session, e1.id, resolver=r)).overall_reachability == "invalid"
    e2 = await make_email(session, local="b", domain="gone-corp.co.uk")
    o2 = await verify_email(session, e2.id, resolver=FakeResolver())
    assert o2.domain_mail_status == "no_mail_service" and o2.overall_reachability == "invalid"


async def test_dns_timeout_is_unknown_not_invalid(session):
    e = await make_email(session, domain="slow-corp.co.uk")
    r = FakeResolver().fail("slow-corp.co.uk", "MX", timeout_exc())
    out = await verify_email(session, e.id, resolver=r)
    assert out.domain_mail_status == "temporary_dns_failure" and out.overall_reachability == "unknown" and out.confidence <= 60


async def test_syntax_invalid_and_disposable_and_free_role(session):
    e = await make_email(session, local="bad..local")
    e.email_original = "bad..local@acme-ltd.co.uk"
    await session.commit()
    out = await verify_email(session, e.id, resolver=good_dns())
    assert out.syntax_status == "invalid" and out.overall_reachability == "invalid" and out.confidence == 0
    d = await make_email(session, local="x", domain="mailinator.com")
    od = await verify_email(session, d.id, resolver=good_dns("mailinator.com"))
    assert od.overall_reachability == "risky" and od.confidence <= 20
    di = await verify_email(session, d.id, options=VerifyOptions(disposable_reachability="invalid"), resolver=good_dns("mailinator.com"))
    assert di.overall_reachability == "invalid"
    f = await make_email(session, local="bob", domain="gmail.com")
    of = await verify_email(session, f.id, resolver=good_dns("gmail.com"))
    await session.refresh(f)
    assert f.is_free_provider and of.overall_reachability == "risky" and of.marketing_status == "consent_required"
    r = await make_email(session, local="info", domain="role-corp.co.uk")
    await verify_email(session, r.id, resolver=good_dns("role-corp.co.uk"))
    await session.refresh(r)
    assert r.role_type == "generic" and r.overall_reachability == "risky"


async def test_bounce_and_suppression_flow(session):
    e = await make_email(session)
    await verify_email(session, e.id, resolver=good_dns())
    await import_delivery_events(session, [{"email": e.email_normalised, "event_type": "hard_bounce", "occurred_at": "2026-03-01T00:00:00Z"}])
    out = await verify_email(session, e.id, mode="deep", resolver=good_dns())
    assert out.overall_reachability == "invalid" and out.marketing_status == "suppressed"
    assert "V10_delivery" in out.checks_run


async def test_company_suppression(session):
    e = await make_email(session)
    org = await session.get(Organisation, e.organisation_id)
    session.add(SuppressionEntry(kind="company", value=org.company_number, reason="objection"))
    await session.commit()
    assert (await verify_email(session, e.id, resolver=good_dns())).marketing_status == "opted_out"


def _factory(server, settings):
    def make(o):
        return SmtpProbe(settings=settings, target_override=lambda h: ("127.0.0.1", server.port), connect_timeout=2, command_timeout=1,
                         max_deferral_retries=0)
    return make


async def test_deep_unauthorised_domain_never_connects(session, smtp_server):
    srv = smtp_server()
    st = Settings(smtp_diagnostics_enabled=True, smtp_authorised_domains=OWNED, smtp_mail_from=f"v@{OWNED}", smtp_helo_host=OWNED)
    e = await make_email(session)  # acme-ltd.co.uk is NOT authorised
    out = await verify_email(session, e.id, mode="deep", resolver=good_dns(), smtp_factory=_factory(srv, st), settings=st)
    assert srv.connections == 0 and srv.verbs == []
    assert out.recipient_evidence_status == "untested" and out.catch_all_status == "untested"
    v7 = (await session.execute(select(ValidationResult).where(ValidationResult.check_type == "V07_smtp"))).scalar_one()
    assert v7.result == "untested" and v7.reason_code == "not_authorised"


async def test_deep_authorised_domain_diagnostics(session, smtp_server):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "250 OK" if a.startswith("jane") else "550 5.1.1 User unknown"
    st = Settings(smtp_diagnostics_enabled=True, smtp_authorised_domains=OWNED, smtp_mail_from=f"v@{OWNED}", smtp_helo_host=OWNED)
    e = await make_email(session, local="jane", domain=OWNED)
    out = await verify_email(session, e.id, mode="deep", resolver=good_dns(OWNED), smtp_factory=_factory(srv, st), settings=st)
    assert out.recipient_evidence_status == "smtp_recipient_accepted"
    assert out.catch_all_status == "rejected_unknown_recipient"
    assert "DATA" not in srv.verbs and srv.verbs.count("RCPT") == 2
    rows = {r.check_type: r for r in (await session.execute(select(ValidationResult).where(ValidationResult.email_id == e.id))).scalars()}
    assert rows["V07_smtp"].response_code == 250 and rows["V07_smtp"].permission_context == "settings_authorised_domains"
    assert rows["V03_mx"].provider is None or isinstance(rows["V03_mx"].provider, str)
    # fresh evidence is reused: no new connections on re-run
    before = srv.connections
    again = await verify_email(session, e.id, mode="deep", resolver=good_dns(OWNED), smtp_factory=_factory(srv, st), settings=st)
    assert srv.connections == before and "V07_smtp" in again.checks_reused
    assert again.recipient_evidence_status == "smtp_recipient_accepted"
    # a FAST run afterwards keeps the (fresh) stored evidence and never claims delivery
    fast = await verify_email(session, e.id, mode="fast", resolver=good_dns(OWNED), settings=st)
    assert fast.recipient_evidence_status == "smtp_recipient_accepted"
    assert fast.overall_reachability in {"safe", "risky", "invalid", "unknown"} and fast.recipient_evidence_status != "deliverable"


async def test_deep_catch_all_domain_is_risky(session, smtp_server):
    srv = smtp_server()  # accepts everything
    st = Settings(smtp_diagnostics_enabled=True, smtp_authorised_domains=OWNED, smtp_mail_from=f"v@{OWNED}", smtp_helo_host=OWNED)
    e = await make_email(session, local="jane", domain=OWNED)
    out = await verify_email(session, e.id, mode="deep", resolver=good_dns(OWNED), smtp_factory=_factory(srv, st), settings=st)
    assert out.catch_all_status == "confirmed" and out.overall_reachability == "risky" and out.confidence <= 60
    assert "DATA" not in srv.verbs


async def test_deep_gateway_provider_suspected_catch_all(session):
    e = await make_email(session, domain="gw-corp.co.uk")
    r = FakeResolver().mail_domain("gw-corp.co.uk", [(10, "mxa-001.pphosted.com")])
    out = await verify_email(session, e.id, mode="deep", resolver=r, settings=Settings())
    assert out.catch_all_status == "suspected" and out.overall_reachability == "risky"


async def test_custom_options_toggle_stages(session):
    e = await make_email(session)
    r = good_dns()
    out = await verify_email(session, e.id, mode="custom", options=VerifyOptions(dns=False, flags=False), resolver=r)
    assert r.calls == [] and "V03_mx" not in out.checks_run and out.domain_mail_status == "unknown"


def test_estimate_cost_text():
    fast = estimate_cost(mode="fast", count=100)
    deep = estimate_cost(VerifyOptions.for_mode("deep"), count=100)
    assert fast["per_email"]["smtp_connections"] == 0 and deep["per_email"]["smtp_connections"] == 2
    assert "DATA" in deep["summary"] and any("not a promise" in x for x in deep["limitations"])
    assert "authorised" in deep["summary"]
