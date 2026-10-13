"""V02/V03/V04 -- domain mail configuration via DNS (MX, implicit MX, null MX, provider).

* DNS timeouts / SERVFAIL are *always* ``temporary_dns_failure`` -- never a permanent "invalid".
* SPF / DMARC lookups are optional **diagnostics** and never influence recipient existence.
* The resolver is injectable: any object with ``async resolve(qname, rdtype, **kw)`` shaped like
  ``dns.asyncresolver.Resolver`` works (tests use a fake -- no network).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DomainCache

MailStatus = Literal["accepts_mail", "no_mail_service", "null_mx", "temporary_dns_failure", "unknown"]

TEMP_FAILURE_TTL = 60
MAX_MX_HOSTS = 8

# MX host suffix -> provider label. Order matters (first match wins).
PROVIDER_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("aspmx.l.google.com", "google_workspace"), ("googlemail.com", "google_workspace"), ("google.com", "google_workspace"),
    ("mail.protection.outlook.com", "microsoft365"), ("outlook.com", "microsoft365"), ("hotmail.com", "microsoft365"),
    ("pphosted.com", "proofpoint"), ("ppe-hosted.com", "proofpoint"), ("proofpoint.com", "proofpoint"),
    ("mimecast.com", "mimecast"), ("mimecast.co.za", "mimecast"), ("mimecast-offshore.com", "mimecast"),
    ("zoho.com", "zoho"), ("zoho.eu", "zoho"), ("zoho.in", "zoho"), ("zohomail.com", "zoho"),
    ("ionos.com", "ionos"), ("ionos.co.uk", "ionos"), ("1and1.com", "ionos"), ("kundenserver.de", "ionos"), ("perfora.net", "ionos"),
    ("secureserver.net", "godaddy"), ("godaddy.com", "godaddy"),
    ("messagingengine.com", "fastmail"), ("fastmail.com", "fastmail"),
    ("protonmail.ch", "proton"), ("proton.me", "proton"),
    ("barracudanetworks.com", "barracuda"), ("barracuda.com", "barracuda"),
    ("mailgun.org", "mailgun"), ("sendgrid.net", "sendgrid"),
    ("registrar-servers.com", "namecheap"), ("privateemail.com", "namecheap"),
    ("yahoodns.net", "yahoo"), ("yahoo.com", "yahoo"),
    ("icloud.com", "apple"), ("me.com", "apple"),
    ("ovh.net", "ovh"), ("mail.ovh.net", "ovh"),
    ("hostinger.com", "hostinger"), ("titan.email", "titan"),
    ("123-reg.co.uk", "123reg"), ("livemail.co.uk", "123reg"),
    ("emailsrvr.com", "rackspace"), ("rackspace.com", "rackspace"),
    ("trendmicro.com", "trendmicro"), ("iphmx.com", "cisco_ironport"), ("mailanyone.net", "mailanyone"),
    ("antispamcloud.com", "spamexperts"), ("spamh.com", "spamexperts"), ("mxthunder.net", "sophos"),
    ("sophos.com", "sophos"), ("cloudflare.net", "cloudflare_email"), ("improvmx.com", "improvmx"), ("forwardemail.net", "forwardemail"),
)


class ResolverLike(Protocol):
    async def resolve(self, qname: str, rdtype: str, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class MxHost:
    preference: int
    host: str
    addresses: list[str] = field(default_factory=list)
    ttl: int | None = None
    resolution: str = "ok"  # ok | nxdomain | no_address | temporary_failure


@dataclass(slots=True)
class DomainMailInfo:
    """Result of the V02/V03/V04 domain mail checks."""

    domain: str
    mail_status: MailStatus
    reason_code: str
    mx: list[MxHost] = field(default_factory=list)
    implicit_mx: bool = False
    implicit_addresses: list[str] = field(default_factory=list)
    provider: str | None = None
    ttl: int = TEMP_FAILURE_TTL
    dnssec: str = "not_checked"
    diagnostics: dict[str, Any] = field(default_factory=dict)  # spf / dmarc presence only; never used for existence
    error: str | None = None
    from_cache: bool = False
    checked_at: datetime | None = None

    def mx_dicts(self) -> list[dict[str, Any]]:
        return [asdict(m) for m in self.mx]

    def mx_hosts_by_priority(self) -> list[MxHost]:
        return sorted(self.mx, key=lambda m: (m.preference, m.host))


def classify_provider(hosts: list[str]) -> str | None:
    """Provider label from MX host suffixes, or ``None`` when self-hosted/unknown."""
    found: list[str] = []
    for h in hosts:
        h = h.lower().rstrip(".")
        for suf, label in PROVIDER_SUFFIXES:
            if h == suf or h.endswith("." + suf):
                found.append(label)
                break
    if not found:
        return None
    return found[0]  # primary (lowest preference) MX host decides; callers pass hosts in priority order


def _clamp(ttl: int | None, floor: int, ceil: int) -> int:
    return max(floor, min(ceil, ttl if ttl is not None else floor))


def _to_ascii(domain: str) -> str:
    d = domain.strip().rstrip(".").lower()
    try:
        return d.encode("idna").decode("ascii")
    except UnicodeError:
        return d


class _Temp(Exception):
    """Temporary DNS failure (timeout / SERVFAIL / no nameservers)."""


async def _query(resolver: ResolverLike, name: str, rdtype: str) -> tuple[list[Any], int | None] | None:
    """Return (rdata list, ttl); ``None`` for NXDOMAIN; ``([], None)`` for NoAnswer; raise _Temp on transient errors."""
    try:
        ans = await resolver.resolve(name, rdtype)
    except dns.resolver.NXDOMAIN:
        return None
    except dns.resolver.NoAnswer:
        return [], None
    except (dns.resolver.NoNameservers, dns.exception.Timeout, dns.resolver.LifetimeTimeout, TimeoutError, OSError) as e:
        raise _Temp(f"{type(e).__name__}: {e}") from e
    except dns.name.EmptyLabel as e:
        raise _Temp(str(e)) from e
    except dns.exception.DNSException as e:
        raise _Temp(f"{type(e).__name__}: {e}") from e
    ttl = getattr(getattr(ans, "rrset", None), "ttl", None)
    return list(ans), ttl


async def _addresses(resolver: ResolverLike, host: str) -> tuple[list[str], int | None, str]:
    addrs: list[str] = []
    ttls: list[int] = []
    states: list[str] = []
    for rt in ("A", "AAAA"):
        try:
            r = await _query(resolver, host, rt)
        except _Temp:
            states.append("temporary_failure")
            continue
        if r is None:
            states.append("nxdomain")
            continue
        recs, ttl = r
        if recs:
            addrs += [x.address for x in recs]
            if ttl is not None:
                ttls.append(ttl)
        states.append("ok" if recs else "no_address")
    if addrs:
        return addrs, (min(ttls) if ttls else None), "ok"
    if "temporary_failure" in states:
        return [], None, "temporary_failure"
    if "nxdomain" in states and all(s == "nxdomain" for s in states):
        return [], None, "nxdomain"
    return [], None, "no_address"


async def _txt_diagnostics(resolver: ResolverLike, domain: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, name, prefix in (("spf", domain, "v=spf1"), ("dmarc", f"_dmarc.{domain}", "v=dmarc1")):
        try:
            r = await _query(resolver, name, "TXT")
        except _Temp:
            out[key] = "lookup_failed"
            continue
        if not r or not r[0]:
            out[key] = "absent"
            continue
        txts = ["".join(s.decode("utf-8", "replace") if isinstance(s, bytes) else str(s) for s in rd.strings).lower() for rd in r[0]]
        out[key] = "present" if any(t.startswith(prefix) for t in txts) else "absent"
    return out


async def lookup_domain_mail(
    domain: str,
    *,
    resolver: ResolverLike | None = None,
    ttl_floor: int = 300,
    ttl_ceil: int = 86400,
    include_diagnostics: bool = False,
    timeout: float = 5.0,
    lifetime: float = 10.0,
) -> DomainMailInfo:
    """Determine whether ``domain`` publishes a usable mail exchanger (RFC 5321 s5.1, RFC 7505)."""
    d = _to_ascii(domain)
    now = datetime.now(UTC)
    if resolver is None:
        r = dns.asyncresolver.Resolver()
        r.timeout, r.lifetime = timeout, lifetime
        resolver = r
    base = {"domain": d, "checked_at": now}
    try:
        mx_res = await _query(resolver, d, "MX")
        if mx_res is None:
            return DomainMailInfo(mail_status="no_mail_service", reason_code="domain_nxdomain", ttl=_clamp(None, ttl_floor, ttl_ceil), **base)
        recs, mx_ttl = mx_res
        if not recs:
            # RFC 5321 s5.1: no MX -> fall back to the address records ("implicit MX")
            addrs, a_ttl, state = await _addresses(resolver, d)
            if addrs:
                info = DomainMailInfo(
                    mail_status="accepts_mail", reason_code="implicit_mx", implicit_mx=True, implicit_addresses=addrs,
                    mx=[MxHost(0, d, addrs, a_ttl)], ttl=_clamp(a_ttl, ttl_floor, ttl_ceil), **base)
            elif state == "temporary_failure":
                return DomainMailInfo(mail_status="temporary_dns_failure", reason_code="dns_temporary_failure", ttl=TEMP_FAILURE_TTL, **base)
            else:
                info = DomainMailInfo(mail_status="no_mail_service", reason_code="no_mx_no_address", ttl=_clamp(None, ttl_floor, ttl_ceil), **base)
        else:
            mxs = sorted(((int(r.preference), str(r.exchange).rstrip(".").lower()) for r in recs), key=lambda t: t[0])
            if len(mxs) == 1 and mxs[0][1] in ("", "."):
                return DomainMailInfo(mail_status="null_mx", reason_code="null_mx_rfc7505", ttl=_clamp(mx_ttl, ttl_floor, ttl_ceil), **base)
            mxs = [(p, h) for p, h in mxs if h not in ("", ".")][:MAX_MX_HOSTS]
            results = await asyncio.gather(*(_addresses(resolver, h) for _, h in mxs))
            hosts = [MxHost(p, h, a, t, st) for (p, h), (a, t, st) in zip(mxs, results, strict=True)]
            resolved = [h for h in hosts if h.addresses]
            ttl_candidates = [t for t in [mx_ttl] + [h.ttl for h in hosts] if t is not None]
            ttl = _clamp(min(ttl_candidates) if ttl_candidates else None, ttl_floor, ttl_ceil)
            if resolved:
                info = DomainMailInfo(
                    mail_status="accepts_mail", reason_code="mx_present", mx=hosts,
                    provider=classify_provider([h.host for h in hosts]), ttl=ttl, **base)
            elif any(h.resolution == "temporary_failure" for h in hosts):
                return DomainMailInfo(mail_status="temporary_dns_failure", reason_code="mx_host_dns_temporary_failure", mx=hosts, ttl=TEMP_FAILURE_TTL, **base)
            else:
                info = DomainMailInfo(
                    mail_status="no_mail_service", reason_code="mx_hosts_unresolvable", mx=hosts,
                    provider=classify_provider([h.host for h in hosts]), ttl=ttl, **base)
        if include_diagnostics:
            info.diagnostics = await _txt_diagnostics(resolver, d)
        return info
    except _Temp as e:
        return DomainMailInfo(mail_status="temporary_dns_failure", reason_code="dns_temporary_failure", error=str(e), ttl=TEMP_FAILURE_TTL, **base)
    except Exception as e:  # noqa: BLE001 - never turn an unexpected error into a permanent verdict
        return DomainMailInfo(mail_status="unknown", reason_code="dns_lookup_error", error=f"{type(e).__name__}: {e}", ttl=TEMP_FAILURE_TTL, **base)


# --------------------------------------------------------------------------- cache + single-flight
CACHE_STATS: dict[str, int] = {"hit": 0, "miss": 0, "expired": 0, "lookups": 0, "singleflight_joined": 0}
_inflight: dict[str, asyncio.Task[DomainMailInfo]] = {}
_sem_state: dict[str, Any] = {"limit": 20, "sem": None, "loop": None}


def configure_concurrency(limit: int) -> None:
    """Set the bound on simultaneous DNS domain lookups (process-wide)."""
    _sem_state.update(limit=max(1, int(limit)), sem=None, loop=None)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if _sem_state["sem"] is None or _sem_state["loop"] is not loop:
        _sem_state["sem"] = asyncio.Semaphore(_sem_state["limit"])
        _sem_state["loop"] = loop
    return _sem_state["sem"]


def reset_stats() -> None:
    for k in CACHE_STATS:
        CACHE_STATS[k] = 0


def _from_row(row: DomainCache) -> DomainMailInfo:
    det = dict(row.detail or {})
    mx = [MxHost(**m) for m in (row.mx or [])]
    return DomainMailInfo(
        domain=row.domain, mail_status=row.mail_status, reason_code=det.get("reason_code", "cached"),  # type: ignore[arg-type]
        mx=mx, implicit_mx=bool(det.get("implicit_mx")), implicit_addresses=list(det.get("implicit_addresses", [])),
        provider=row.provider, ttl=int(det.get("ttl", TEMP_FAILURE_TTL)), dnssec=det.get("dnssec", "not_checked"),
        diagnostics=det.get("diagnostics", {}), error=det.get("error"), from_cache=True, checked_at=row.checked_at)


async def _store(session: AsyncSession, info: DomainMailInfo) -> None:
    now = info.checked_at or datetime.now(UTC)
    detail = {
        "reason_code": info.reason_code, "implicit_mx": info.implicit_mx, "implicit_addresses": info.implicit_addresses,
        "ttl": info.ttl, "dnssec": info.dnssec, "diagnostics": info.diagnostics, "error": info.error,
    }
    vals = {
        "domain": info.domain, "mail_status": info.mail_status, "mx": info.mx_dicts(), "provider": info.provider,
        "detail": detail, "checked_at": now, "expires_at": now + timedelta(seconds=info.ttl),
    }
    stmt = pg_insert(DomainCache).values(**vals)
    stmt = stmt.on_conflict_do_update(index_elements=["domain"], set_={k: v for k, v in vals.items() if k != "domain"})
    await session.execute(stmt)
    await session.commit()


async def get_domain_info(
    session: AsyncSession,
    domain: str,
    *,
    resolver: ResolverLike | None = None,
    ttl_floor: int = 300,
    ttl_ceil: int = 86400,
    force_refresh: bool = False,
    include_diagnostics: bool = False,
    max_age: int | None = None,
) -> DomainMailInfo:
    """Cached domain lookup. Concurrent calls for one domain share a single DNS lookup (single-flight)."""
    d = _to_ascii(domain)
    if not force_refresh:
        row = await session.get(DomainCache, d)
        if row is not None:
            now = datetime.now(UTC)
            fresh = row.expires_at > now and (max_age is None or (now - row.checked_at).total_seconds() <= max_age)
            if fresh:
                CACHE_STATS["hit"] += 1
                return _from_row(row)
            CACHE_STATS["expired"] += 1
    CACHE_STATS["miss"] += 1

    task = _inflight.get(d)
    leader = task is None
    if leader:
        async def _run() -> DomainMailInfo:
            async with _semaphore():
                CACHE_STATS["lookups"] += 1
                return await lookup_domain_mail(d, resolver=resolver, ttl_floor=ttl_floor, ttl_ceil=ttl_ceil, include_diagnostics=include_diagnostics)

        task = asyncio.ensure_future(_run())
        _inflight[d] = task
        task.add_done_callback(lambda _t, k=d: _inflight.pop(k, None))
    else:
        CACHE_STATS["singleflight_joined"] += 1
    assert task is not None
    info = await asyncio.shield(task)
    if leader:
        await _store(session, info)
    return info
