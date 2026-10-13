"""Test doubles: fake DNS resolver (no network) shaped like dns.asyncresolver.Resolver."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import dns.exception
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.resolver


class FakeResolver:
    def __init__(self) -> None:
        self.zone: dict[tuple[str, str], Any] = {}
        self.names: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.delay = 0.0

    def add(self, name: str, rdtype: str, texts: list[str], ttl: int = 300) -> FakeResolver:
        n = name.lower().rstrip(".")
        self.zone[(n, rdtype)] = (ttl, texts)
        self.names.add(n)
        return self

    def fail(self, name: str, rdtype: str, exc: Exception) -> FakeResolver:
        n = name.lower().rstrip(".")
        self.zone[(n, rdtype)] = exc
        self.names.add(n)
        return self

    def mail_domain(self, domain: str, mx: list[tuple[int, str]], ip: str = "192.0.2.10", ttl: int = 300) -> FakeResolver:
        self.add(domain, "MX", [f"{p} {h}." for p, h in mx], ttl)
        for _, h in mx:
            self.add(h, "A", [ip], ttl)
        return self

    async def resolve(self, qname: str, rdtype: str, *a: Any, **k: Any) -> Any:
        import asyncio
        if self.delay:
            await asyncio.sleep(self.delay)
        n = str(qname).lower().rstrip(".")
        self.calls.append((n, rdtype))
        v = self.zone.get((n, rdtype))
        if isinstance(v, Exception):
            raise v
        if v is None:
            if n in self.names:
                raise dns.resolver.NoAnswer()
            raise dns.resolver.NXDOMAIN()
        ttl, texts = v
        rds = [dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rdtype), t) for t in texts]
        ans = list(rds)

        class _A(list):
            pass

        out = _A(ans)
        out.rrset = SimpleNamespace(ttl=ttl)  # type: ignore[attr-defined]
        return out


def timeout_exc() -> Exception:
    return dns.resolver.LifetimeTimeout(timeout=1.0, errors=[])
