"""SSRF-safe HTTP fetching.

Design
------
* Every URL (the first one and every redirect hop) is validated *before* a request is made:
  scheme, no credentials, port allow-list, numeric-IP obfuscation, IDN -> punycode.
* DNS is resolved by us. Every resolved address must be public; if ANY is private/loopback/
  link-local/multicast/reserved/CGNAT/metadata the whole host is refused.
* The TCP connection is made to the validated IP (``PinnedBackend``) while the Host header and
  TLS SNI/certificate verification keep using the original host name. Because the check and the
  connect happen on the same lookup, DNS rebinding between "check" and "use" is impossible.
* Redirects are followed manually (so each hop is re-validated), with loop detection.
* Bodies are streamed with a byte cap and a content-type allow-list; downloads are aborted early.
* ``settings.allow_private_networks`` disables ONLY the address/port checks (tests against local
  fixture servers). Scheme, credentials and size/type limits always apply.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpcore
import httpx

from ..config import Settings, get_settings
from .urls import is_ip_literal, is_obfuscated_numeric_host, normalise_url, to_ascii_host

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- error codes
DNS_FAILURE = "dns_failure"
TLS_FAILURE = "tls_failure"
TIMEOUT = "timeout"
BLOCKED_SSRF = "blocked_ssrf"
TOO_LARGE = "too_large"
BAD_CONTENT_TYPE = "bad_content_type"
REDIRECT_LOOP = "redirect_loop"
CONNECTION_ERROR = "connection_error"
INVALID_URL = "invalid_url"
ERROR_CODES = frozenset(
    {DNS_FAILURE, TLS_FAILURE, TIMEOUT, BLOCKED_SSRF, TOO_LARGE, BAD_CONTENT_TYPE, REDIRECT_LOOP,
     CONNECTION_ERROR, INVALID_URL}
)

DEFAULT_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
DOCUMENT_TYPES = frozenset(
    {
        "text/html", "application/xhtml+xml", "text/plain", "application/pdf", "text/vcard",
        "text/x-vcard", "application/xml", "text/xml", "application/json",
    }
)
# text-like types for which a size-truncated body is still useful
PARTIAL_OK_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain", "text/vcard", "text/x-vcard"})
SITEMAP_TYPES = frozenset({"application/xml", "text/xml", "text/plain", "application/gzip", "application/x-gzip",
                           "application/octet-stream", "text/html"})
ROBOTS_TYPES = frozenset({"text/plain", "text/html", "application/octet-stream", "text/x-robots"})
_OCTET_OK_SUFFIXES = (".pdf", ".vcf", ".gz")

_KEEP_HEADERS = (
    "content-type", "content-length", "content-encoding", "content-language", "content-disposition", "etag",
    "last-modified", "location", "retry-after", "server", "x-robots-tag", "cache-control", "date",
)


class SSRFBlocked(Exception):
    """A URL or resolved address was refused by the SSRF policy."""


class DnsFailure(Exception):
    """Host name did not resolve."""


# --------------------------------------------------------------------------- IP policy
_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        # IPv4
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
        # IPv6
        "::/8", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:2::/48", "2001:10::/28",
        "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8", "fd00:ec2::/32",
    )
]
_METADATA_ADDRS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
                   ipaddress.ip_address("100.100.100.200")}


def classify_ip(addr: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a reason string when the address must not be contacted, else None."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0] if isinstance(addr, str) else addr)
    except ValueError:
        return "unparseable address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        inner = classify_ip(ip.ipv4_mapped)
        return f"ipv4-mapped: {inner}" if inner else "ipv4-mapped address"
    if ip in _METADATA_ADDRS:
        return "cloud metadata address"
    for net in _BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return f"reserved/private range {net}"
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return "non-global address"
    return None


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP), timeout=8.0
        )
    except TimeoutError as e:
        raise DnsFailure(f"dns timeout for {host}") from e
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise DnsFailure(f"cannot resolve {host}: {e}") from e
    seen: dict[str, None] = {}
    for fam, _, _, _, sa in infos:
        if fam in (socket.AF_INET, socket.AF_INET6):
            seen.setdefault(sa[0], None)
    addrs = list(seen)
    # IPv4 first: deterministic, and most hosts are dual-stack
    addrs.sort(key=lambda a: ":" in a)
    return addrs


@dataclass(frozen=True, slots=True)
class Target:
    """A validated request target (no DNS performed yet)."""

    url: str  # canonical, rebuilt from validated parts
    scheme: str
    host: str  # ascii/punycode, no brackets
    port: int


class Guard:
    """URL + address policy. Shared by the fetcher, the pinned connector and the JS renderer."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
        allow_private: Callable[[], bool] | bool = False,
    ) -> None:
        self.resolver = resolver or system_resolver
        self.allowed_ports = frozenset(allowed_ports)
        self._allow_private = allow_private if callable(allow_private) else (lambda: allow_private)

    @property
    def allow_private(self) -> bool:
        return bool(self._allow_private())

    def check_url(self, url: str) -> Target:
        """Syntactic validation. Raises SSRFBlocked (policy) or ValueError (malformed)."""
        if not isinstance(url, str) or not url.strip():
            raise ValueError("empty url")
        url = url.strip()
        if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url) or "\\" in url:
            raise ValueError("control characters / backslash in url")
        try:
            parts = urlsplit(url)
        except ValueError as e:
            raise ValueError(f"unparseable url: {e}") from e
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise SSRFBlocked(f"scheme {scheme or '(none)'!r} not allowed")
        netloc = parts.netloc
        if "@" in netloc:
            raise SSRFBlocked("credentials in url are not allowed")
        try:
            port = parts.port
        except ValueError as e:
            raise ValueError("bad port") from e
        raw_host = parts.hostname
        if not raw_host:
            raise ValueError("missing host")
        if "%" in raw_host and ":" not in raw_host:
            raise SSRFBlocked("percent-encoded host")
        host = raw_host.lower().rstrip(".")
        if is_obfuscated_numeric_host(host):
            raise SSRFBlocked("non-canonical numeric IP host")
        ascii_host = to_ascii_host(host)
        if not ascii_host:
            raise ValueError("invalid host")
        if not is_ip_literal(ascii_host) and not re.fullmatch(r"[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?)*", ascii_host):
            raise ValueError("invalid host characters")
        eff_port = port or (443 if scheme == "https" else 80)
        if not self.allow_private:
            if eff_port not in self.allowed_ports:
                raise SSRFBlocked(f"port {eff_port} not in allow-list")
            if is_ip_literal(ascii_host):
                reason = classify_ip(ascii_host)
                if reason:
                    raise SSRFBlocked(f"address {ascii_host} refused: {reason}")
            if ascii_host in ("localhost",) or ascii_host.endswith((".localhost", ".local", ".internal")):
                raise SSRFBlocked("local host name")
        default = 443 if scheme == "https" else 80
        netloc_out = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
        if eff_port != default:
            netloc_out += f":{eff_port}"
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~[]") if parts.query else ""
        out = f"{scheme}://{netloc_out}{path}" + (f"?{query}" if query else "")
        return Target(out, scheme, ascii_host, eff_port)

    async def resolve(self, host: str, port: int) -> list[str]:
        """Resolve and validate. Raises SSRFBlocked if ANY address is not public."""
        if is_ip_literal(host):
            addrs = [host.split("%")[0]]
        else:
            addrs = await self.resolver(host, port)
            if not addrs:
                raise DnsFailure(f"no addresses for {host}")
        if not self.allow_private:
            for a in addrs:
                reason = classify_ip(a)
                if reason:
                    raise SSRFBlocked(f"{host} resolves to {a}: {reason}")
        return addrs


class PinnedBackend(httpcore.AnyIOBackend):
    """Network backend that connects to the *validated* IP of the host (DNS-rebinding safe).

    httpcore still uses the original host for the Host header, SNI and certificate checks.
    """

    def __init__(self, guard: Guard) -> None:
        super().__init__()
        self._guard = guard

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addrs = await self._guard.resolve(host, port)
        last: Exception | None = None
        for ip in addrs:
            try:
                return await super().connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                 socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last


# --------------------------------------------------------------------------- conditional-request cache
@dataclass(slots=True)
class CacheEntry:
    url: str
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    status: int | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None


class CacheStore(Protocol):
    async def get(self, url: str) -> CacheEntry | None: ...
    async def put(self, entry: CacheEntry) -> None: ...


def url_hash(url: str) -> str:
    return hashlib.sha256((normalise_url(url) or url).encode()).hexdigest()


class MemoryCacheStore:
    def __init__(self) -> None:
        self._d: dict[str, CacheEntry] = {}

    async def get(self, url: str) -> CacheEntry | None:
        return self._d.get(url_hash(url))

    async def put(self, entry: CacheEntry) -> None:
        self._d[url_hash(entry.url)] = entry


class DbCacheStore:
    """Persists validators in ``crawl_cache`` using short, independent sessions."""

    def __init__(self, sessionmaker=None) -> None:
        self._sm = sessionmaker

    def _factory(self):
        if self._sm is not None:
            return self._sm
        from .. import db

        return db.sessionmaker()

    async def get(self, url: str) -> CacheEntry | None:
        from ..models import CrawlCache

        async with self._factory()() as s:
            row = await s.get(CrawlCache, url_hash(url))
            if row is None:
                return None
            return CacheEntry(row.url, row.etag, row.last_modified, row.content_hash, row.status,
                              row.fetched_at, row.expires_at)

    async def put(self, entry: CacheEntry) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from ..models import CrawlCache

        now = datetime.now(UTC)
        vals = {
            "url_hash": url_hash(entry.url), "url": entry.url, "etag": (entry.etag or None) and entry.etag[:300],
            "last_modified": (entry.last_modified or None) and entry.last_modified[:100],
            "content_hash": entry.content_hash, "status": entry.status, "fetched_at": entry.fetched_at or now,
            "expires_at": entry.expires_at or now + timedelta(days=1),
        }
        stmt = insert(CrawlCache).values(**vals)
        stmt = stmt.on_conflict_do_update(index_elements=["url_hash"], set_={k: v for k, v in vals.items() if k != "url_hash"})
        async with self._factory()() as s:
            await s.execute(stmt)
            await s.commit()


# --------------------------------------------------------------------------- result type
@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    content_type: str | None = None
    truncated: bool = False
    from_cache: bool = False
    not_modified: bool = False
    error_code: str | None = None
    error_detail: str | None = None
    redirects: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    content_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None and (self.not_modified or (self.status is not None and 200 <= self.status < 300))

    @property
    def usable_body(self) -> bool:
        """Body present and worth parsing (complete, or a truncated text page)."""
        return bool(self.body) and (self.error_code is None or (self.error_code == TOO_LARGE and self.truncated))

    def text(self) -> str:
        return decode_body(self.body, self.headers.get("content-type"))

    def retry_after(self) -> float | None:
        from .ratelimit import parse_retry_after

        return parse_retry_after(self.headers.get("retry-after"))


# --------------------------------------------------------------------------- decoding
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_CT_CHARSET = re.compile(r"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-:.]+)", re.I)


def decode_body(body: bytes, content_type: str | None = None) -> str:
    """Bytes -> str without ever raising: BOM, header charset, <meta>, utf-8, cp1252, latin-1."""
    if not body:
        return ""
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", "replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return body.decode("utf-16")
        except UnicodeError:
            pass
    candidates: list[str] = []
    if content_type:
        m = _CT_CHARSET.search(content_type)
        if m:
            candidates.append(m.group(1))
    m2 = _META_CHARSET.search(body[:4096])
    if m2:
        candidates.append(m2.group(1).decode("ascii", "ignore"))
    for cs in candidates:
        try:
            return body.decode(cs.strip(), "strict")
        except (LookupError, UnicodeError):
            continue
    try:
        return body.decode("utf-8")
    except UnicodeError:
        pass
    for cs in candidates:  # declared but with a few bad bytes: lenient decode beats mojibake
        try:
            return body.decode(cs.strip(), "replace")
        except (LookupError, UnicodeError):
            continue
    try:
        return body.decode("cp1252")
    except UnicodeError:
        return body.decode("latin-1", "replace")


def _base_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _walk_causes(exc: BaseException):
    seen = 0
    while exc is not None and seen < 8:
        yield exc
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
        seen += 1


def classify_exception(exc: BaseException) -> tuple[str, str]:
    for e in _walk_causes(exc):
        if isinstance(e, SSRFBlocked):
            return BLOCKED_SSRF, str(e)
        if isinstance(e, DnsFailure):
            return DNS_FAILURE, str(e)
        if isinstance(e, ssl.SSLError | ssl.CertificateError):
            return TLS_FAILURE, str(e)
    if isinstance(exc, httpx.TimeoutException | TimeoutError | asyncio.TimeoutError):
        return TIMEOUT, type(exc).__name__
    if isinstance(exc, httpx.InvalidURL):
        return INVALID_URL, str(exc)
    msg = str(exc)
    if "SSL" in msg or "CERTIFICATE" in msg.upper():
        return TLS_FAILURE, msg
    if "Name or service not known" in msg or "getaddrinfo" in msg or "nodename nor servname" in msg:
        return DNS_FAILURE, msg
    return CONNECTION_ERROR, f"{type(exc).__name__}: {msg}"[:300]


# --------------------------------------------------------------------------- fetcher
class SafeFetcher:
    """Connection-pooled, SSRF-safe, size/type-bounded HTTP fetcher.

    ``fetch`` never raises (except cancellation): all failures come back as ``FetchResult.error_code``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
        max_redirects: int = 5,
        allow_private_networks: bool | None = None,
        max_connections: int = 100,
        verify: bool | ssl.SSLContext = True,
        cache: CacheStore | None = None,
        total_timeout_factor: float = 3.0,
    ) -> None:
        self._settings = settings or get_settings()
        s = self._settings
        if allow_private_networks is None:
            allow_private = lambda: bool(s.allow_private_networks)  # noqa: E731 - read live (tests monkeypatch)
        else:
            allow_private = allow_private_networks
        self.guard = Guard(resolver=resolver, allowed_ports=allowed_ports, allow_private=allow_private)
        self.max_redirects = max_redirects
        self.cache = cache
        self._total_timeout = max(1.0, s.http_timeout_s * total_timeout_factor)
        timeout = httpx.Timeout(s.http_timeout_s, connect=min(10.0, s.http_timeout_s))
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max(10, max_connections // 5),
                              keepalive_expiry=20.0)
        if transport is None:
            at = httpx.AsyncHTTPTransport(verify=verify, limits=limits, http1=True, http2=False, trust_env=False)
            at._pool._network_backend = PinnedBackend(self.guard)  # type: ignore[attr-defined]
            transport = at
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False, headers=self._base_headers()
        )
        self.stats = {"requests": 0, "bytes": 0, "errors": 0, "blocked": 0}

    # -- identity
    @property
    def user_agent(self) -> str:
        ua = self._settings.user_agent
        email = (self._settings.contact_email or "").strip()
        if email and email not in ua:
            ua = f"{ua} (contact: {email})"
        return ua

    @property
    def robots_token(self) -> str:
        return self.user_agent.split("/")[0].split(" ")[0] or "LeadIntelBot"

    def _base_headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", "Accept-Language": "en;q=0.9,*;q=0.5",
                "Connection": "keep-alive"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeFetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- public API
    def validate_url(self, url: str) -> str:
        """Return canonical URL or raise SSRFBlocked / ValueError (no DNS)."""
        return self.guard.check_url(url).url

    async def fetch(
        self,
        url: str,
        *,
        accept: Iterable[str] | None = None,
        max_bytes: int | None = None,
        cache: CacheStore | None = None,
        conditional: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        t0 = time.monotonic()
        res = FetchResult(url=url, final_url=url)
        try:
            async with asyncio.timeout(self._total_timeout):
                await self._fetch_chain(res, url, frozenset(accept) if accept else DOCUMENT_TYPES,
                                        max_bytes or self._settings.http_max_bytes,
                                        cache if cache is not None else self.cache, conditional, extra_headers)
        except TimeoutError:
            self._fail(res, TIMEOUT, "overall fetch deadline exceeded")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leak
            code, detail = classify_exception(e)
            self._fail(res, code, detail)
        res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        self.stats["requests"] += 1
        self.stats["bytes"] += len(res.body)
        if res.error_code:
            self.stats["errors"] += 1
            if res.error_code == BLOCKED_SSRF:
                self.stats["blocked"] += 1
        return res

    # -- internals
    @staticmethod
    def _fail(res: FetchResult, code: str, detail: str | None = None) -> None:
        res.error_code = code
        res.error_detail = detail

    async def _fetch_chain(self, res: FetchResult, url: str, accept: frozenset[str], max_bytes: int,
                           cache: CacheStore | None, conditional: bool, extra_headers: dict[str, str] | None) -> None:
        current = url
        visited: list[str] = []
        for hop in range(self.max_redirects + 1):
            try:
                target = self.guard.check_url(current)
            except SSRFBlocked as e:
                return self._fail(res, BLOCKED_SSRF, str(e))
            except ValueError as e:
                return self._fail(res, INVALID_URL, str(e))
            canon = target.url
            if canon in visited:
                return self._fail(res, REDIRECT_LOOP, f"redirect loop at {canon}")
            visited.append(canon)
            res.final_url = canon
            headers: dict[str, str] = {"Accept": ", ".join(sorted(accept)) if len(accept) < 12 else "*/*"}
            if extra_headers:
                headers.update(extra_headers)
            entry = None
            if cache is not None and conditional:
                try:
                    entry = await cache.get(canon)
                except Exception as e:
                    log.debug("crawl cache get failed: %s", e)
                if entry and entry.etag:
                    headers["If-None-Match"] = entry.etag
                if entry and entry.last_modified:
                    headers["If-Modified-Since"] = entry.last_modified
            try:
                done = await self._one_request(res, canon, headers, accept, max_bytes, entry)
            except SSRFBlocked as e:
                return self._fail(res, BLOCKED_SSRF, str(e))
            if done:
                if (res.error_code is None and cache is not None and res.status == 200 and not res.truncated
                        and (res.headers.get("etag") or res.headers.get("last-modified"))):
                    now = datetime.now(UTC)
                    try:
                        await cache.put(CacheEntry(canon, res.headers.get("etag"), res.headers.get("last-modified"),
                                                   res.content_hash, 200, now, now + timedelta(days=1)))
                    except Exception as e:
                        log.debug("crawl cache put failed: %s", e)
                return
            # redirect: res.headers["location"] holds the raw Location
            loc = res.headers.get("location", "")
            res.redirects.append(canon)
            try:
                current = urljoin(canon, loc.strip())
            except ValueError:
                return self._fail(res, INVALID_URL, "bad Location header")
        self._fail(res, REDIRECT_LOOP, f"more than {self.max_redirects} redirects")

    async def _one_request(self, res: FetchResult, url: str, headers: dict[str, str], accept: frozenset[str],
                           max_bytes: int, entry: CacheEntry | None) -> bool:
        """Perform one hop. Returns True when ``res`` is final, False to follow ``Location``."""
        try:
            async with self._client.stream("GET", url, headers=headers) as resp:
                res.status = resp.status_code
                res.headers = {k: v for k, v in ((k.lower(), v) for k, v in resp.headers.items()) if k in _KEEP_HEADERS}
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    return False
                ctype = _base_type(resp.headers.get("content-type"))
                res.content_type = ctype
                if resp.status_code == 304:
                    if entry is not None:
                        res.not_modified = True
                        res.from_cache = True
                        res.content_hash = entry.content_hash
                    return True
                if resp.status_code >= 300:
                    return True  # error/other status: no body is read
                if ctype is None:
                    ctype = "text/html"
                    res.content_type = ctype
                if ctype not in accept and not (
                    ctype == "application/octet-stream" and urlsplit(url).path.lower().endswith(_OCTET_OK_SUFFIXES)
                    and "application/pdf" in accept
                ):
                    self._fail(res, BAD_CONTENT_TYPE, f"content-type {ctype} not allowed")
                    return True
                declared = resp.headers.get("content-length")
                partial_ok = ctype in PARTIAL_OK_TYPES
                if declared and declared.isdigit() and int(declared) > max_bytes and not partial_ok:
                    res.truncated = True
                    self._fail(res, TOO_LARGE, f"content-length {declared} exceeds {max_bytes}")
                    return True
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        del buf[max_bytes:]
                        res.truncated = True
                        self._fail(res, TOO_LARGE, f"body exceeds {max_bytes} bytes")
                        break
                if res.error_code == TOO_LARGE and not partial_ok:
                    buf.clear()
                res.body = bytes(buf)
                res.content_hash = hashlib.sha256(res.body).hexdigest()
                return True
        except httpx.DecodingError as e:
            self._fail(res, CONNECTION_ERROR, f"content decoding failed: {e}")
            return True
        except SSRFBlocked:
            raise
        except (httpx.HTTPError, DnsFailure, ssl.SSLError, OSError, httpcore.ProxyError) as e:
            code, detail = classify_exception(e)
            self._fail(res, code, detail)
            return True
