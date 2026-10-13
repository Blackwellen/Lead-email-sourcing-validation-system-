"""Resolver sanity check: detect DNS infrastructure that cannot answer MX queries.

Some sandboxes, captive networks and misconfigured resolvers answer A/AAAA queries but return no MX records at all.
Per RFC 5321 a domain without MX falls back to its address records ("implicit MX"), so on such a resolver EVERY domain
would look mail-capable. We probe a few domains that are known to publish MX records; if none of them returns any, the
resolver is flagged and implicit-MX results are reported as ``unknown`` instead of ``accepts_mail``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import dns.asyncresolver
import dns.exception
import dns.resolver

# Large mail providers that have published MX records for decades (used only as a resolver self-test).
PROBE_DOMAINS = ("gmail.com", "outlook.com", "yahoo.com")
CACHE_SECONDS = 600


@dataclass
class ResolverHealth:
    status: str  # ok | no_mx_answers | failing
    detail: dict[str, Any] = field(default_factory=dict)
    checked_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "checked_at": self.checked_at}


_cache: dict[str, ResolverHealth] = {}


async def check_resolver(resolver: Any = None, *, force: bool = False) -> ResolverHealth:
    hit = _cache.get("system")
    if hit is not None and not force and time.time() - hit.checked_at < CACHE_SECONDS:
        return hit
    if resolver is None:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout, resolver.lifetime = 3.0, 6.0
    answers, errors = {}, {}
    for d in PROBE_DOMAINS:
        try:
            ans = await resolver.resolve(d, "MX")
            answers[d] = len(list(ans))
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            answers[d] = 0
        except (dns.exception.DNSException, OSError) as e:
            errors[d] = type(e).__name__
    if any(n > 0 for n in answers.values()):
        status = "ok"
    elif answers:
        status = "no_mx_answers"
    else:
        status = "failing"
    h = ResolverHealth(status, {"mx_answer_counts": answers, "errors": errors, "probe_domains": list(PROBE_DOMAINS)}, time.time())
    _cache["system"] = h
    return h


def reset_cache() -> None:
    _cache.clear()
