import pytest

from app.config import Settings
from app.models import DomainPermission
from app.verify import smtp as smtpmod
from app.verify.catchall import assess_catch_all
from app.verify.dnsmx import DomainMailInfo, MxHost
from app.verify.scoring import ScoreInputs, score_email
from app.verify.smtp import ALLOWED_VERBS, SmtpProbe, is_probe_authorised

from .conftest import OWNED
from .smtpserver import closed_port

ADDR = f"jane@{OWNED}"
HOSTS = [f"mx1.{OWNED}"]


def assert_no_data(server, res):
    assert "DATA" not in server.verbs and "DATA-HANDLED" not in server.verbs
    assert set(res.verbs_sent) <= ALLOWED_VERBS
    assert not {"DATA", "BDAT", "VRFY", "EXPN"} & set(server.verbs)


async def test_accepted_is_not_deliverable(session, smtp_server, make_probe):
    srv = smtp_server()
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.status == "completed"
    assert res.recipient_evidence_status == "smtp_recipient_accepted"
    assert res.response_code == 250 and res.enhanced_status_code == "2.1.5"
    assert res.permission_context == "settings_authorised_domains"
    assert {"EHLO", "MAIL", "RCPT", "RSET", "QUIT"} <= set(srv.verbs)
    assert_no_data(srv, res)
    assert any(t["stage"] == "rcpt" and t["code"] == 250 for t in res.transcript)
    assert "deliver" not in res.reason_code
    # even with the best possible other evidence, a speculative address is never 'safe'
    sc = score_email(ScoreInputs(origin="pattern_inferred", domain_mail_status="accepts_mail", recipient_evidence_status=res.recipient_evidence_status,
                                 domain_identity_confidence=1.0, age_days=1))
    assert sc.reachability != "safe" and sc.score <= 55


async def test_nonexistent_rejected(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "550 5.1.1 <x>: Recipient address rejected: User unknown"
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "smtp_recipient_rejected"
    assert res.response_code == 550 and res.enhanced_status_code == "5.1.1"
    assert_no_data(srv, res)


async def test_catch_all_confirmed_stays_risky(session, smtp_server, make_probe):
    srv = smtp_server()  # accepts everything
    probe = make_probe(srv)
    info = DomainMailInfo(OWNED, "accepts_mail", "mx_present", mx=[MxHost(10, HOSTS[0], ["192.0.2.1"])])
    ca = await assess_catch_all(session, OWNED, info, probe=probe)
    assert ca.status == "confirmed" and ca.basis == "active_probe"
    assert srv.scenario.rcpt_addresses[0].startswith("zz-nx-")
    sc = score_email(ScoreInputs(origin="published", source_kind="own_domain_page", domain_identity_confidence=1, contact_confidence=1,
                                 domain_mail_status="accepts_mail", recipient_evidence_status="smtp_recipient_accepted", catch_all_status=ca.status, age_days=1))
    assert sc.reachability == "risky" and sc.score <= 60
    assert "DATA" not in srv.verbs


async def test_catch_all_rejecting_server(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "550 5.1.1 no such user"
    info = DomainMailInfo(OWNED, "accepts_mail", "mx_present", mx=[MxHost(10, HOSTS[0], ["192.0.2.1"])])
    ca = await assess_catch_all(session, OWNED, info, probe=make_probe(srv))
    assert ca.status == "rejected_unknown_recipient"


async def test_greylist_then_accept(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "450 4.2.0 Greylisted, try again later" if n == 1 else "250 OK"
    p = make_probe(srv, backoff_base=2.0, backoff_max=10.0)
    res = await p.probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "smtp_recipient_accepted"
    assert res.attempts == 2 and p.sleeps == [2.0]
    assert_no_data(srv, res)


async def test_temporary_451_stays_inconclusive_with_bounded_backoff(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "451 4.3.0 Temporary local problem"
    p = make_probe(srv, max_deferral_retries=2, backoff_base=1.0, backoff_max=1.5)
    res = await p.probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "inconclusive"
    assert res.reply_category == "deferred" and res.attempts == 3
    assert p.sleeps == [1.0, 1.5]  # exponential, capped
    assert srv.scenario.rcpt_attempts[ADDR] == 3


async def test_mailbox_full_inconclusive(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "452 4.2.2 Mailbox full"
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "inconclusive" and res.reason_code == "smtp_mailbox_full"
    assert res.attempts == 1  # not retried as a greylist


async def test_mailbox_disabled(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "550 5.2.1 The email account that you tried to reach is disabled"
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "smtp_recipient_rejected" and res.reason_code == "smtp_mailbox_disabled"


async def test_policy_rejection_not_invalid(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_rule = lambda a, n: "550 5.7.1 Service unavailable; client host blocked using Spamhaus"
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.status == "policy_restricted" and res.recipient_evidence_status == "inconclusive"


async def test_starttls_failure_recorded_then_plain_retry(session, smtp_server, make_probe):
    srv = smtp_server(with_tls=True)
    srv.scenario.starttls = "fail"
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.tls.get("tls_failed") is True
    assert res.recipient_evidence_status == "smtp_recipient_accepted"
    assert srv.connections == 2
    assert_no_data(srv, res)


async def test_starttls_success_records_tls(session, smtp_server, make_probe):
    srv = smtp_server(with_tls=True)
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.tls.get("started") is True
    assert res.tls["tls_version"].startswith("TLS")
    assert res.recipient_evidence_status == "smtp_recipient_accepted"
    assert srv.verbs.count("EHLO") == 2 and "STARTTLS" in srv.verbs


async def test_timeout_inconclusive(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_delay = 5
    res = await make_probe(srv, command_timeout=0.3).probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "inconclusive" and res.reason_code == "smtp_command_timeout"


async def test_mid_session_disconnect(session, smtp_server, make_probe):
    srv = smtp_server()
    srv.scenario.rcpt_disconnect = True
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "inconclusive" and res.reason_code == "smtp_connection_lost"


async def test_accept_then_bounce_is_only_acceptance(session, smtp_server, make_probe):
    """Server accepts at RCPT (and would bounce later). We never send DATA, so all we can say is 'accepted'."""
    srv = smtp_server()
    res = await make_probe(srv).probe(session, ADDR, HOSTS)
    assert res.recipient_evidence_status == "smtp_recipient_accepted"
    assert "DATA" not in srv.verbs
    assert "deliver" not in str(res.detail()["note"]).lower().replace("not proof of delivery", "")


async def test_mx_fallback_on_connection_refused(session, smtp_server, make_probe, probe_settings):
    srv = smtp_server()
    dead = closed_port()
    p = SmtpProbe(settings=probe_settings, connect_timeout=1, command_timeout=1,
                  target_override=lambda h: ("127.0.0.1", dead if h.startswith("dead") else srv.port))
    res = await p.probe(session, ADDR, ["dead.mx", "live.mx"])
    assert res.recipient_evidence_status == "smtp_recipient_accepted" and res.mx_host == "live.mx"


async def test_unauthorised_domain_makes_no_connection(session, smtp_server, make_probe):
    srv = smtp_server()
    res = await make_probe(srv).probe(session, "bob@thirdparty-company.co.uk", ["mx.thirdparty-company.co.uk"])
    assert res.status == "not_permitted" and res.reason_code == "not_authorised"
    assert res.recipient_evidence_status == "untested"
    assert srv.connections == 0 and srv.verbs == []
    # passive catch-all for an unauthorised domain: untested, no connection
    info = DomainMailInfo("thirdparty-company.co.uk", "accepts_mail", "mx_present", mx=[MxHost(10, "mx.thirdparty-company.co.uk", ["192.0.2.1"])])
    ca = await assess_catch_all(session, "thirdparty-company.co.uk", info, probe=make_probe(srv))
    assert ca.status == "untested" and srv.connections == 0


async def test_diagnostics_disabled_makes_no_connection(session, smtp_server):
    srv = smtp_server()
    st = Settings(smtp_diagnostics_enabled=False, smtp_authorised_domains=OWNED, smtp_mail_from=f"v@{OWNED}")
    p = SmtpProbe(settings=st, target_override=lambda h: ("127.0.0.1", srv.port))
    res = await p.probe(session, ADDR, HOSTS)
    assert res.status == "not_permitted" and srv.connections == 0


async def test_domain_permission_row_authorises(session, smtp_server):
    srv = smtp_server()
    st = Settings(smtp_diagnostics_enabled=True, smtp_authorised_domains="", smtp_mail_from=f"v@{OWNED}", smtp_helo_host=OWNED)
    p = SmtpProbe(settings=st, target_override=lambda h: ("127.0.0.1", srv.port), connect_timeout=2, command_timeout=1)
    assert not await is_probe_authorised(session, "perm-domain.co.uk", settings=st)
    session.add(DomainPermission(domain="perm-domain.co.uk", smtp_probe_allowed=True, basis="written consent"))
    await session.commit()
    a = await is_probe_authorised(session, "perm-domain.co.uk", settings=st)
    assert a and a.context == "domain_permission_row"
    res = await p.probe(session, "x@perm-domain.co.uk", ["mx.perm-domain.co.uk"])
    assert res.permission_context == "domain_permission_row" and res.recipient_evidence_status == "smtp_recipient_accepted"
    # a row with smtp_probe_allowed False does not authorise
    session.add(DomainPermission(domain="no.co.uk", smtp_probe_allowed=False, basis="x"))
    await session.commit()
    assert not await is_probe_authorised(session, "no.co.uk", settings=st)


async def test_data_verb_is_structurally_impossible(smtp_server):
    p = SmtpProbe(settings=Settings(smtp_mail_from=f"v@{OWNED}"))

    class W:
        def write(self, b): raise AssertionError("must not write")
        async def drain(self): ...

    for verb in ("DATA", "BDAT 10", "VRFY a", "EXPN b", "AUTH PLAIN"):
        with pytest.raises(AssertionError):
            await p._send(W(), verb)
    src = open(smtpmod.__file__).read()
    assert 'f"DATA' not in src and '"DATA' not in src.replace('"DATA", "BDAT"', "")
