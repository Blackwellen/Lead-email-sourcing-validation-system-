"""URL helpers shared by crawler and identity code (pure, no I/O)."""
from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import tldextract

_TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "_ga", "yclid", "igshid", "ref_src"}
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]*))*$", re.I)


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # Bundled snapshot only: never touches the network. Private suffixes (blogspot.com,
    # github.io ...) count as public suffixes so tenants are distinct registrants.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


@lru_cache(maxsize=1)
def known_tlds() -> frozenset[str]:
    ex = _extractor()
    out: set[str] = set()
    for t in ex.tlds:
        if "." in t:
            continue
        t = t.lower()
        out.add(t)
        if not t.isascii():
            try:
                out.add(t.encode("idna").decode("ascii"))
            except UnicodeError:
                pass
    return frozenset(out)


def to_ascii_host(host: str) -> str | None:
    """Lower-case, strip dots/brackets and convert IDN to punycode. None when impossible."""
    host = (host or "").strip().strip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except UnicodeError:
        return None
    if len(ascii_host) > 253 or any(not (0 < len(lab) <= 63) for lab in ascii_host.split(".")):
        return None
    return ascii_host


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_obfuscated_numeric_host(host: str) -> bool:
    """True for decimal/hex/octal/short-form IPv4 encodings (``2130706433``, ``0x7f.1``)."""
    h = host.strip("[]").lower()
    if is_ip_literal(h):
        return False
    return bool(_NUMERIC_HOST.match(h))


def registrable_domain(value: str | None) -> str | None:
    """Registrable domain (eTLD+1) of a host, URL or e-mail address, as punycode.

    Returns None for IP addresses, single-label hosts, or unknown suffixes.
    ``www.`` equivalence falls out naturally.
    """
    if not value:
        return None
    v = value.strip()
    if "@" in v and "://" not in v:
        v = v.rsplit("@", 1)[1]
    if "://" in v:
        v = urlsplit(v).hostname or ""
    else:
        v = v.split("/")[0].split("?")[0]
        if v.count(":") == 1:
            v = v.split(":")[0]
    host = to_ascii_host(v)
    if not host or is_ip_literal(host):
        return None
    try:
        ext = _extractor()(host)
    except Exception:
        return None
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}".lower()


def host_of(url: str) -> str | None:
    try:
        h = urlsplit(url).hostname
    except ValueError:
        return None
    return to_ascii_host(h) if h else None


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def site_key(url_or_host: str) -> str | None:
    """Key used for same-site comparison: registrable domain, or the exact host for IPs/localhost."""
    host = host_of(url_or_host) if "://" in url_or_host else to_ascii_host(url_or_host.split("/")[0].split(":")[0])
    if not host:
        return None
    return registrable_domain(host) or strip_www(host)


def same_site(url: str, other: str) -> bool:
    a, b = site_key(url), site_key(other)
    return a is not None and a == b


def normalise_url(url: str, *, drop_query: bool = False) -> str | None:
    """Canonical form used for de-duplication; None if not an http(s) URL."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = to_ascii_host(parts.hostname)
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = ""
    if parts.query and not drop_query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        ]
        query = urlencode(kept)
    return urlunsplit((scheme, netloc, path, query, ""))
