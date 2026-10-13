"""Resolver self-test: a resolver that never returns MX must not make every domain look mail-capable."""
from app.verify import dns_health

from .fakes import FakeResolver


async def test_healthy_resolver():
    dns_health.reset_cache()
    r = FakeResolver().mail_domain("gmail.com", [(5, "gmail-smtp-in.l.google.com")])
    h = await dns_health.check_resolver(r, force=True)
    assert h.ok and h.detail["mx_answer_counts"]["gmail.com"] == 1


async def test_resolver_without_mx_answers_is_flagged():
    dns_health.reset_cache()
    r = FakeResolver()
    for d in dns_health.PROBE_DOMAINS:
        r.add(d, "A", ["192.0.2.1"])  # names exist, MX queries return NoAnswer
    h = await dns_health.check_resolver(r, force=True)
    assert h.status == "no_mx_answers" and not h.ok


async def test_cached_until_forced():
    dns_health.reset_cache()
    r = FakeResolver().mail_domain("gmail.com", [(5, "mx.example.net")])
    await dns_health.check_resolver(r, force=True)
    assert (await dns_health.check_resolver(FakeResolver())).ok  # served from cache
