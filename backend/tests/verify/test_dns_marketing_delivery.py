import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import dns.resolver
from sqlalchemy import select

from app.models import DeliveryEvent, DomainCache, EmailAddress, Organisation, SuppressionEntry
from app.verify import dnsmx
from app.verify.delivery import import_delivery_events
from app.verify.dnsmx import classify_provider, get_domain_info, lookup_domain_mail
from app.verify.marketing import marketing_decision, marketing_status

from .fakes import FakeResolver, timeout_exc


# ------------------------------------------------------------------ DNS classification
async def test_mx_accepts_mail_with_provider():
    r = FakeResolver().mail_domain("acme-ltd.co.uk", [(20, "alt1.aspmx.l.google.com"), (10, "aspmx.l.google.com")], ttl=600)
    info = await lookup_domain_mail("acme-ltd.co.uk", resolver=r)
    assert info.mail_status == "accepts_mail" and info.provider == "google_workspace"
    assert [m.preference for m in info.mx_hosts_by_priority()] == [10, 20]
    assert info.mx[0].addresses == ["192.0.2.10"] and info.ttl == 600 and info.dnssec == "not_checked"


async def test_null_mx():
    r = FakeResolver().add("nomail.co.uk", "MX", ["0 ."])
    assert (await lookup_domain_mail("nomail.co.uk", resolver=r)).mail_status == "null_mx"


async def test_nxdomain():
    info = await lookup_domain_mail("doesnotexist.co.uk", resolver=FakeResolver())
    assert info.mail_status == "no_mail_service" and info.reason_code == "domain_nxdomain"


async def test_timeout_is_temporary_never_invalid():
    r = FakeResolver().fail("slow.co.uk", "MX", timeout_exc())
    info = await lookup_domain_mail("slow.co.uk", resolver=r)
    assert info.mail_status == "temporary_dns_failure"
    r2 = FakeResolver().fail("servfail.co.uk", "MX", dns.resolver.NoNameservers())
    assert (await lookup_domain_mail("servfail.co.uk", resolver=r2)).mail_status == "temporary_dns_failure"


async def test_implicit_mx_a_fallback():
    r = FakeResolver().add("web-only.co.uk", "A", ["198.51.100.7"])
    info = await lookup_domain_mail("web-only.co.uk", resolver=r)
    assert info.mail_status == "accepts_mail" and info.implicit_mx and info.implicit_addresses == ["198.51.100.7"]


async def test_no_mx_no_address():
    r = FakeResolver().add("txtonly.co.uk", "TXT", ['"hello"'])
    info = await lookup_domain_mail("txtonly.co.uk", resolver=r)
    assert info.mail_status == "no_mail_service" and info.reason_code == "no_mx_no_address"


async def test_mx_host_unresolvable_and_temporary():
    r = FakeResolver().add("broken.co.uk", "MX", ["10 gone.broken.co.uk."])
    assert (await lookup_domain_mail("broken.co.uk", resolver=r)).mail_status == "no_mail_service"
    r2 = FakeResolver().add("flaky.co.uk", "MX", ["10 mx.flaky.co.uk."]).fail("mx.flaky.co.uk", "A", timeout_exc()).fail("mx.flaky.co.uk", "AAAA", timeout_exc())
    assert (await lookup_domain_mail("flaky.co.uk", resolver=r2)).mail_status == "temporary_dns_failure"


async def test_idna_domain_queried_as_ascii():
    r = FakeResolver().mail_domain("xn--bcher-kva.de", [(10, "mx.xn--bcher-kva.de")])
    info = await lookup_domain_mail("bücher.de", resolver=r)
    assert info.domain == "xn--bcher-kva.de" and info.mail_status == "accepts_mail"


def test_provider_classification():
    cases = {
        "x.mail.protection.outlook.com": "microsoft365", "mxa-001.pphosted.com": "proofpoint", "us-smtp-inbound-1.mimecast.com": "mimecast",
        "mx.zoho.com": "zoho", "mx00.ionos.co.uk": "ionos", "mailstore1.secureserver.net": "godaddy", "in1-smtp.messagingengine.com": "fastmail",
        "aspmx.l.google.com": "google_workspace",
    }
    for host, prov in cases.items():
        assert classify_provider([host]) == prov, host
    assert classify_provider(["mail.selfhosted.co.uk"]) is None


async def test_spf_dmarc_are_diagnostics_only():
    r = FakeResolver().mail_domain("diag.co.uk", [(10, "mx.diag.co.uk")]).add("diag.co.uk", "TXT", ['"v=spf1 -all"'])
    info = await lookup_domain_mail("diag.co.uk", resolver=r, include_diagnostics=True)
    assert info.diagnostics == {"spf": "present", "dmarc": "absent"} and info.mail_status == "accepts_mail"


# ------------------------------------------------------------------ cache
async def test_domain_cache_hit_miss_and_expiry(session):
    dnsmx.reset_stats()
    r = FakeResolver().mail_domain("cached.co.uk", [(10, "mx.cached.co.uk")], ttl=300)
    a = await get_domain_info(session, "cached.co.uk", resolver=r)
    assert not a.from_cache and dnsmx.CACHE_STATS["miss"] == 1
    n_calls = len(r.calls)
    b = await get_domain_info(session, "cached.co.uk", resolver=r)
    assert b.from_cache and b.mail_status == "accepts_mail" and len(r.calls) == n_calls
    assert dnsmx.CACHE_STATS["hit"] == 1 and b.mx[0].host == "mx.cached.co.uk"
    row = await session.get(DomainCache, "cached.co.uk")
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    c = await get_domain_info(session, "cached.co.uk", resolver=r)
    assert not c.from_cache and len(r.calls) > n_calls and dnsmx.CACHE_STATS["expired"] == 1


async def test_temp_failure_cached_short(session):
    r = FakeResolver().fail("t.co.uk", "MX", timeout_exc())
    await get_domain_info(session, "t.co.uk", resolver=r)
    row = await session.get(DomainCache, "t.co.uk")
    assert row.mail_status == "temporary_dns_failure"
    assert (row.expires_at - row.checked_at).total_seconds() <= 60


async def test_single_flight_dedup(session):
    from app.db import sessionmaker
    dnsmx.reset_stats()
    r = FakeResolver().mail_domain("once.co.uk", [(10, "mx.once.co.uk")])
    r.delay = 0.05

    async def one():
        async with sessionmaker()() as s:
            return await get_domain_info(s, "once.co.uk", resolver=r)

    res = await asyncio.gather(*(one() for _ in range(8)))
    assert all(x.mail_status == "accepts_mail" for x in res)
    assert dnsmx.CACHE_STATS["lookups"] == 1 and dnsmx.CACHE_STATS["singleflight_joined"] >= 1
    assert r.calls.count(("once.co.uk", "MX")) == 1


# ------------------------------------------------------------------ marketing
def mk(**kw):
    base = dict(email_normalised="jane@acme-ltd.co.uk", domain="acme-ltd.co.uk", registrable_domain="acme-ltd.co.uk", role_type=None, is_free_provider=False)
    base.update(kw)
    return SimpleNamespace(**base)


def org(**kw):
    base = dict(id=7, country_code="GB", legal_type="Private limited company", company_number="01234567", canonical_identity="gb:01234567",
                legal_name="Acme Ltd", trading_name=None)
    base.update(kw)
    return SimpleNamespace(**base)


def sup(kind, value, reason="manual"):
    return SimpleNamespace(kind=kind, value=value, reason=reason)


def test_marketing_defaults_never_eligible():
    assert marketing_status(mk(), org(), [], None) == "needs_review"
    assert marketing_status(mk(role_type="generic"), org(), [], None) == "needs_review"
    assert marketing_status(mk(), org(country_code="DE"), [], None) == "needs_review"
    assert marketing_status(mk(), org(country_code="US"), [], None) == "needs_review"


def test_marketing_consent_required_sole_trader_and_free():
    assert marketing_status(mk(), org(legal_type="Sole trader"), [], None) == "consent_required"
    assert marketing_status(mk(email_normalised="a@gmail.com", domain="gmail.com", is_free_provider=True), org(), [], None) == "consent_required"
    assert marketing_status(mk(), org(legal_type="Limited Liability Partnership"), [], None) == "needs_review"
    assert marketing_status(mk(), org(country_code="FR", legal_type="individual entrepreneur"), [], None) == "consent_required"


def test_marketing_eligible_only_by_operator_policy():
    pol = {"GB": {"personal": "eligible"}}
    assert marketing_status(mk(), org(), [], {"marketing_policy": pol}) == "needs_review"  # allow_eligible missing
    pol = {"GB": {"personal": "eligible", "allow_eligible": True}}
    assert marketing_status(mk(), org(), [], SimpleNamespace(marketing_policy=pol)) == "eligible"
    assert marketing_status(mk(), org(), [sup("email", "jane@acme-ltd.co.uk", "bounce")], SimpleNamespace(marketing_policy=pol)) == "suppressed"


def test_marketing_suppression_matching():
    assert marketing_status(mk(), org(), [sup("email", "jane@acme-ltd.co.uk", "opt_out")]) == "opted_out"
    assert marketing_status(mk(), org(), [sup("email", "jane@acme-ltd.co.uk", "objection")]) == "opted_out"
    assert marketing_status(mk(), org(), [sup("domain", "acme-ltd.co.uk", "manual")]) == "suppressed"
    assert marketing_status(mk(domain="mail.acme-ltd.co.uk"), org(), [sup("domain", "acme-ltd.co.uk", "legal")]) == "suppressed"
    assert marketing_status(mk(), org(), [sup("company", "01234567", "complaint")]) == "suppressed"
    assert marketing_status(mk(), org(), [sup("email", "other@acme-ltd.co.uk")]) == "needs_review"
    assert marketing_status(mk(role_type="noreply"), org(), []) == "not_permitted"
    assert "not legal advice" in marketing_decision(mk(), org(), []).note.lower()


# ------------------------------------------------------------------ delivery import
async def _email(session, addr="jane@acme-ltd.co.uk"):
    o = Organisation(legal_name="Acme", country_code="GB", canonical_identity=f"x:{addr}")
    session.add(o)
    await session.flush()
    e = EmailAddress(organisation_id=o.id, email_original=addr, email_normalised=addr, local_part=addr.split("@")[0], domain=addr.split("@")[1],
                     registrable_domain=addr.split("@")[1], source_type="web")
    session.add(e)
    await session.commit()
    return e


async def test_delivery_import_idempotent_and_suppresses(session):
    e = await _email(session)
    rows = [
        {"email": "Jane@Acme-Ltd.co.uk", "event_type": "hard_bounce", "campaign": "c1", "occurred_at": "2026-01-02T10:00:00Z", "detail": "550 user unknown"},
        {"email": "jane@acme-ltd.co.uk", "event_type": "delivered", "campaign": "c0", "occurred_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"email": "jane@acme-ltd.co.uk", "event_type": "spam", "campaign": "c1", "occurred_at": "2026-01-03T10:00:00Z"},
        {"email": "jane@acme-ltd.co.uk", "event_type": "unsubscribe", "occurred_at": "2026-01-04T10:00:00Z"},
        {"email": "nobody@acme-ltd.co.uk", "event_type": "hard_bounce", "occurred_at": "2026-01-04T10:00:00Z"},
        {"email": "jane@acme-ltd.co.uk", "event_type": "weird", "occurred_at": "2026-01-04T10:00:00Z"},
    ]
    s1 = await import_delivery_events(session, rows)
    assert (s1.inserted, s1.unknown_email, s1.invalid, s1.suppressions_added) == (4, 1, 1, 3)
    s2 = await import_delivery_events(session, rows)
    assert s2.inserted == 0 and s2.duplicates == 4 and s2.suppressions_added == 0
    assert len((await session.execute(select(DeliveryEvent))).scalars().all()) == 4
    reasons = {x.reason for x in (await session.execute(select(SuppressionEntry))).scalars()}
    assert reasons == {"opt_out"}  # strongest reason wins on the single (email) suppression row
    await session.refresh(e)
    assert e.previously_bounced is True and e.suppressed_at is not None


async def test_accepted_event_does_not_flag(session):
    e = await _email(session)
    await import_delivery_events(session, [{"email": e.email_normalised, "event_type": "accepted", "occurred_at": "2026-02-01T00:00:00Z"}])
    await session.refresh(e)
    assert e.previously_bounced is False and e.suppressed_at is None
    assert (await session.execute(select(SuppressionEntry))).first() is None
