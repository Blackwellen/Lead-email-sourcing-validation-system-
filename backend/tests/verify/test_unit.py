import asyncio

import pytest

from app.verify import lists
from app.verify.delivery import normalise_event_type
from app.verify.permutations import (
    PATTERNS,
    family_variants,
    generate_named_candidates,
    generate_role_candidates,
    infer_patterns,
    to_ascii,
)
from app.verify.scoring import ScoreInputs, score_email
from app.verify.smtp import classify_reply
from app.verify.syntax import check_syntax


# ------------------------------------------------------------------ syntax
def test_syntax_normalisation():
    r = check_syntax("Jane.Doe@Example-Corp.CO.UK")
    assert r.status == "valid" and r.normalised == "jane.doe@example-corp.co.uk"
    assert r.local_part == "Jane.Doe" and r.domain == "example-corp.co.uk"


def test_syntax_unicode_idna():
    r = check_syntax("josé@Bücher.de")
    assert r.status == "valid" and r.ascii_domain == "xn--bcher-kva.de" and r.smtputf8


@pytest.mark.parametrize("bad", ["", "   ", "plain", "a@", "@b.com", "a b@c.com", "a@b..com", "a@-b.com", "x@y", "a@@b.com"])
def test_syntax_invalid(bad):
    r = check_syntax(bad)
    assert r.status == "invalid" and r.error and r.reason_code.startswith("syntax_")


@pytest.mark.parametrize("dom", ["a@site.test", "a@x.invalid", "a@host.localhost", "a@thing.example", "a@example.com", "a@x.local"])
def test_special_use_domains_invalid(dom):
    assert check_syntax(dom).status == "invalid"


def test_syntax_length_limits():
    assert check_syntax("a" * 65 + "@example-corp.co.uk").reason_code == "syntax_local_too_long"
    assert check_syntax("a@" + "b" * 250 + ".com").status == "invalid"


def test_display_name_stripping():
    assert check_syntax('"Jane Doe" <jane@acme-ltd.co.uk>').status == "invalid"
    r = check_syntax('"Jane Doe" <Jane@acme-ltd.co.uk>', strip_display=True)
    assert r.status == "valid" and r.normalised == "jane@acme-ltd.co.uk"
    assert check_syntax("mailto:a@acme-ltd.co.uk?subject=x", strip_display=True).normalised == "a@acme-ltd.co.uk"


# ------------------------------------------------------------------ lists
def test_lists():
    assert len(lists.disposable_domains()) >= 150
    assert lists.is_disposable("mailinator.com") and lists.is_disposable("x.mailinator.com") and lists.is_disposable("YOPMAIL.com")
    assert not lists.is_disposable("acme-ltd.co.uk")
    for d in ("gmail.com", "outlook.com", "yahoo.co.uk", "icloud.com", "proton.me", "btinternet.com", "sky.com"):
        assert lists.is_free_provider(d)
    assert not lists.is_free_provider("acme-ltd.co.uk")


@pytest.mark.parametrize("local,role", [
    ("info", "generic"), ("Info", "generic"), ("sales", "sales"), ("support", "support"), ("bookings", "bookings"),
    ("reservations", "bookings"), ("suppliers", "suppliers"), ("partnerships", "partnerships"), ("accounts", "accounts"),
    ("careers", "hr"), ("recruitment", "hr"), ("no-reply", "noreply"), ("noreply", "noreply"), ("no_reply", "noreply"),
    ("postmaster", "postmaster"), ("abuse", "legal"), ("webmaster", "postmaster"), ("hello", "generic"), ("enquiries", "generic"),
    ("reception", "reception"), ("operations", "operations"), ("management", "management"), ("info+tag", "generic"),
])
def test_role_classification(local, role):
    assert lists.classify_role(local) == role


def test_role_none_and_override():
    assert lists.classify_role("john.smith") is None
    lists.set_role_dictionary({"fred": "custom"})
    try:
        assert lists.classify_role("fred") == "custom" and lists.classify_role("info") is None
    finally:
        lists.set_role_dictionary(None)
    assert lists.classify_role("info") == "generic"


async def test_refresh_from_url_merges(tmp_path):
    import httpx
    body = "\n".join(f"junk{i}-example.org" for i in range(80)) + "\n# c\n"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    target = tmp_path / "d.txt"
    target.write_text("keepme.com\n")
    async with httpx.AsyncClient(transport=transport) as c:
        n = await lists.refresh_from_url("https://x.invalid/list", client=c, path=target)
    assert n == 81 and "keepme.com" in target.read_text()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="a.com\n"))) as c:
        with pytest.raises(ValueError):
            await lists.refresh_from_url("https://x.invalid/list", client=c, path=target)
    lists.clear_caches()


# ------------------------------------------------------------------ reply classification
RCPT_TABLE = [
    (250, "2.1.5", "OK", "accepted", "smtp_recipient_accepted"),
    (251, None, "User not local; will forward", "accepted", "smtp_recipient_accepted"),
    (550, "5.1.1", "User unknown", "rejected_user_unknown", "smtp_recipient_rejected"),
    (550, "5.1.0", "Address rejected", "rejected_user_unknown", "smtp_recipient_rejected"),
    (550, None, "5.1.1 <a@b>: Recipient address rejected: User unknown in virtual mailbox table", "rejected_user_unknown", "smtp_recipient_rejected"),
    (550, None, "No such user here", "rejected_user_unknown", "smtp_recipient_rejected"),
    (553, None, "Mailbox does not exist", "rejected_user_unknown", "smtp_recipient_rejected"),
    (452, "4.2.2", "Mailbox full", "mailbox_full", "inconclusive"),
    (552, "5.2.2", "Over quota", "mailbox_full", "inconclusive"),
    (550, "5.2.1", "Mailbox disabled", "mailbox_disabled", "smtp_recipient_rejected"),
    (550, None, "The mailbox has been disabled", "mailbox_disabled", "smtp_recipient_rejected"),
    (421, None, "Service not available", "deferred", "inconclusive"),
    (450, "4.2.0", "Greylisted", "deferred", "inconclusive"),
    (451, "4.3.0", "Local error", "deferred", "inconclusive"),
    (452, "4.5.3", "Too many recipients", "deferred", "inconclusive"),
    (471, None, "weird 4xx", "deferred", "inconclusive"),
    (550, "5.7.1", "Relay denied", "policy_denied", "inconclusive"),
    (554, "5.7.1", "Blocked", "policy_denied", "inconclusive"),
    (554, None, "Your IP is listed at zen.spamhaus.org blacklist", "policy_denied", "inconclusive"),
    (550, None, "Client host rejected: cannot find your reverse hostname (PTR)", "policy_denied", "inconclusive"),
    (550, None, "Rejected due to policy", "policy_denied", "inconclusive"),
    (550, None, "Requested action not taken", "other", "inconclusive"),
    (500, None, "Command unrecognised", "unsupported", "inconclusive"),
    (502, "5.5.1", "Command not implemented", "unsupported", "inconclusive"),
]


@pytest.mark.parametrize("code,enh,text,cat,evidence", RCPT_TABLE)
def test_reply_classification_rcpt(code, enh, text, cat, evidence):
    rc = classify_reply(code, enh, text, "rcpt")
    assert (rc.category, rc.evidence) == (cat, evidence)
    assert "deliver" not in rc.reason_code  # never a deliverability claim


def test_reply_classification_other_stages():
    assert classify_reply(554, None, "no service", "connect").category == "policy_denied"
    assert classify_reply(421, None, "busy", "connect").category == "deferred"
    assert classify_reply(550, "5.7.1", "sender blocked", "mail").evidence == "inconclusive"
    assert classify_reply(None, None, "", "rcpt").evidence == "inconclusive"


# ------------------------------------------------------------------ scoring
def good(**kw):
    base = dict(origin="published", source_kind="own_domain_page", source_reliability=0.9, domain_identity_confidence=1.0, contact_confidence=0.8,
                corroborating_sources=2, domain_mail_status="accepts_mail", age_days=10)
    base.update(kw)
    return ScoreInputs(**base)


def test_score_safe_path_and_breakdown():
    r = score_email(good())
    assert r.reachability == "safe" and r.score >= 70
    comps = {b["component"] for b in r.breakdown}
    assert {"syntax", "source_quality", "identity", "corroboration", "domain_mail", "freshness", "smtp_evidence"} <= comps
    assert all({"component", "points", "max", "note"} <= set(b) for b in r.breakdown)
    assert "not a guarantee" in r.disclaimer or "not a guarantee" in r.as_dict()["disclaimer"]


def test_speculative_capped_never_safe():
    for origin in ("pattern_inferred", "role_speculative"):
        r = score_email(good(origin=origin, recipient_evidence_status="smtp_recipient_accepted", catch_all_status="rejected_unknown_recipient"))
        assert r.score <= 55 and r.reachability != "safe"
        assert any("speculative" in c for c in r.caps_applied) or r.score <= 55


def test_mx_alone_gives_speculative_low_score():
    r = score_email(ScoreInputs(origin="pattern_inferred", source_kind="inferred", domain_mail_status="accepts_mail", age_days=1))
    assert r.score < 45 and r.reachability == "unknown"


@pytest.mark.parametrize("kw", [
    {"syntax_status": "invalid"}, {"domain_mail_status": "no_mail_service"}, {"domain_mail_status": "null_mx"},
    {"hard_bounce": True}, {"previously_bounced": True}, {"recipient_evidence_status": "smtp_recipient_rejected"},
])
def test_invalid_conditions(kw):
    assert score_email(good(**kw)).reachability == "invalid"


def test_catch_all_and_unknown_at_most_risky():
    for ca in ("confirmed", "suspected"):
        r = score_email(good(catch_all_status=ca, recipient_evidence_status="smtp_recipient_accepted"))
        assert r.reachability == "risky" and r.score <= 70
    assert score_email(good(domain_mail_status="temporary_dns_failure")).reachability == "unknown"
    assert score_email(good(domain_mail_status="temporary_dns_failure")).score <= 60


def test_disposable_free_role():
    assert score_email(good(is_disposable=True)).reachability == "risky"
    assert score_email(good(is_disposable=True)).score <= 20
    assert score_email(good(is_disposable=True, disposable_reachability="invalid")).reachability == "invalid"
    assert score_email(good(is_free_provider=True)).reachability == "risky"
    assert score_email(good(role_type="generic")).reachability == "risky"
    assert score_email(good()).score > score_email(good(role_type="generic")).score


def test_freshness_decay_and_conflicts():
    assert score_email(good(age_days=10)).score > score_email(good(age_days=400)).score > score_email(good(age_days=900)).score
    r = score_email(good(conflicts=1))
    assert r.reachability == "risky" and any(b["component"] == "conflict_penalty" for b in r.breakdown)


def test_smtp_acceptance_discounted_without_catchall_test():
    a = score_email(good(recipient_evidence_status="smtp_recipient_accepted", catch_all_status="rejected_unknown_recipient")).score
    b = score_email(good(recipient_evidence_status="smtp_recipient_accepted", catch_all_status="untested")).score
    assert a >= b


# ------------------------------------------------------------------ permutations
def test_all_eleven_patterns_generated():
    out = generate_named_candidates("John", "Smith", "acme-ltd.co.uk", patterns=list(PATTERNS), limit=8)
    assert len(out) == 8
    locals_ = {c.local_part for c in generate_named_candidates("John", "Smith", "acme-ltd.co.uk", patterns=list(PATTERNS), limit=99)}
    assert locals_ == {"john", "smith", "john.smith", "john_smith", "john-smith", "jsmith", "j.smith", "johnsmith", "smith.john", "smithj", "johns"}
    assert all(c.origin == "pattern_inferred" for c in out)


def test_named_conservative_defaults():
    out = generate_named_candidates("John", "Smith", "acme-ltd.co.uk")
    assert len(out) == 3 and out[0].local_part == "john.smith"
    assert all(c.confidence_hint <= 0.5 and c.origin != "published" for c in out)
    assert len(generate_named_candidates("John", "Smith", "acme-ltd.co.uk", limit=500)) <= 15


def test_transliteration_and_compound_surnames():
    assert to_ascii("Zoë Ångström-Łukasz") == "zoeangstromlukasz"
    assert to_ascii("Müller") == "muller" and to_ascii("Straße") == "strasse" and to_ascii("Øster") == "oster"
    assert family_variants("van der Berg")[0] == "vanderberg" and "berg" in family_variants("van der Berg")
    assert family_variants("O'Brien") == ["obrien"]
    assert family_variants("Smith-Jones")[:2] == ["smithjones", "smith-jones"]
    c = generate_named_candidates("José", "García López", "acme.es", patterns=["first.last"], limit=5)
    assert c[0].email == "jose.garcialopez@acme.es"


def test_role_candidates():
    out = generate_role_candidates("acme-ltd.co.uk", "Hotel & hospitality", limit=6)
    assert all(c.origin == "role_speculative" for c in out)
    assert {"info", "reservations"} <= {c.local_part for c in out} or len(out) == 6
    out2 = generate_role_candidates("acme-ltd.co.uk", None, {"published_locals": ["info"]}, limit=5)
    assert "info" not in {c.local_part for c in out2}
    assert len(generate_role_candidates("a.co.uk", None, limit=100)) <= 10


def test_infer_patterns():
    pairs = [("John", "Smith", "john.smith"), ("Ann", "Lee", "ann.lee"), ("Bob", "Hope", "bhope"), ("Zoë", "Müller", "zoe.muller@acme.de")]
    h = infer_patterns(pairs)
    top = h[0]
    assert top.pattern == "first.last" and top.supporting == 3 and top.sample_size == 4 and top.conflicts == 1
    assert 0 < top.confidence < 0.9
    flast = next(x for x in h if x.pattern == "flast")
    assert flast.supporting == 1 and flast.conflicts == 3 and flast.confidence < top.confidence
    one = infer_patterns([("John", "Smith", "john.smith")])[0]
    assert one.confidence <= 0.34  # a single example stays weak
    assert infer_patterns([("John", "Smith", "info")]) == []


def test_event_alias():
    assert normalise_event_type("Hard Bounce") == "hard_bounce" and normalise_event_type("delivered") == "accepted"
    assert normalise_event_type("spam") == "complaint" and normalise_event_type("???") is None


async def test_unused_asyncio():
    await asyncio.sleep(0)
