"""Syntactic URL / domain validation helpers (no network access, ever)."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import tldextract

# Offline: use the suffix list snapshot bundled with tldextract; never fetch one at runtime.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_RESERVED_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".invalid", ".intranet", ".arpa", ".onion",
)
_CTRL_RE = re.compile(r"[\x00-\x20\x7f<>\"'`\\^{}|]")
MAX_URL_LEN = 2048


@dataclass(frozen=True)
class NormalisedSite:
    url: str  # canonical scheme://host[/path] (no fragment)
    host: str  # lower-case, IDNA (punycode) host without trailing dot
    registrable: str  # eTLD+1 (public-suffix aware, offline snapshot)


def normalise_website(raw: str | None, *, default_scheme: str = "https") -> NormalisedSite | None:
    """Return a normalised site or ``None`` if ``raw`` is not a syntactically valid public web address."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > MAX_URL_LEN or _CTRL_RE.search(s):
        return None
    if "://" not in s:
        if s.startswith("//"):
            s = default_scheme + ":" + s
        elif re.match(r"^[a-z][a-z0-9+.-]*:", s, re.I) and not re.match(r"^[^/]+:\d+(/|$)", s):
            return None  # mailto:, javascript:, data: ...
        else:
            s = f"{default_scheme}://{s}"
    try:
        parts = urlsplit(s)
        host = parts.hostname
        _ = parts.port  # validates the port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password:
        return None
    host = host.strip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(host) > 253 or "." not in host or host == "localhost" or host.endswith(_RESERVED_SUFFIXES):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if not all(_LABEL_RE.match(lbl) for lbl in labels):
        return None
    ext = _EXTRACT(host)
    if not ext.suffix or not ext.domain:
        return None
    registrable = f"{ext.domain}.{ext.suffix}"
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = parts.path if parts.path not in ("", "/") else ""
    url = urlunsplit((parts.scheme.lower(), host + port, path, parts.query, ""))
    return NormalisedSite(url=url, host=host, registrable=registrable)


def normalise_domain(raw: str | None) -> NormalisedSite | None:
    """Like :func:`normalise_website` but for a bare domain cell (paths/queries dropped)."""
    site = normalise_website(raw)
    if site is None:
        return None
    return NormalisedSite(url=f"https://{site.host}", host=site.host, registrable=site.registrable)
