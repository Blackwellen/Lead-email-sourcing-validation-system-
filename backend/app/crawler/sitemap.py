"""Sitemap / sitemap-index parsing (bounded, gz-aware, XXE-safe) and contact-page selection."""
from __future__ import annotations

import io
import re
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from lxml import etree

from .extract import rank_internal_links
from .types import RankedLink
from .urls import normalise_url

MAX_DECOMPRESSED = 10 * 1024 * 1024
_LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)\s*(?:\]\]>)?\s*</loc>", re.I)


@dataclass(slots=True)
class SitemapParse:
    urls: list[str] = field(default_factory=list)  # page URLs (normalised, de-duplicated)
    sitemaps: list[str] = field(default_factory=list)  # child sitemaps from an index
    truncated: bool = False
    error: str | None = None

    @property
    def is_index(self) -> bool:
        return bool(self.sitemaps) and not self.urls


def _gunzip_bounded(data: bytes, limit: int) -> tuple[bytes, bool]:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error:
        # maybe raw deflate or truncated gzip: return whatever decoded
        return b"", False
    return out[:limit], len(out) > limit


def parse_sitemap(data: bytes, *, max_urls: int = 5000, max_bytes: int = MAX_DECOMPRESSED) -> SitemapParse:
    """Parse sitemap XML (optionally gzipped), a sitemap index, or a plain-text URL list. Never raises."""
    res = SitemapParse()
    if not data:
        res.error = "empty"
        return res
    try:
        if data[:2] == b"\x1f\x8b":
            data, over = _gunzip_bounded(data, max_bytes)
            res.truncated = over
            if not data:
                res.error = "bad gzip"
                return res
        elif len(data) > max_bytes:
            data, res.truncated = data[:max_bytes], True
        seen: set[str] = set()
        seen_maps: set[str] = set()
        stripped = data.lstrip()[:200].lower()
        if not stripped.startswith(b"<") and b"<urlset" not in stripped:
            # plain text sitemap: one URL per line
            for line in data.decode("utf-8", "replace").splitlines():
                u = normalise_url(line.strip())
                if u and u not in seen:
                    seen.add(u)
                    res.urls.append(u)
                    if len(res.urls) >= max_urls:
                        res.truncated = True
                        break
            return res
        try:
            ctx = etree.iterparse(io.BytesIO(data), events=("end",), resolve_entities=False, load_dtd=False,
                                  no_network=True, recover=True, huge_tree=False)
            for _ev, el in ctx:
                local = etree.QName(el).localname if isinstance(el.tag, str) else ""
                if local == "loc":
                    parent = el.getparent()
                    ptag = etree.QName(parent).localname if parent is not None and isinstance(parent.tag, str) else ""
                    u = normalise_url((el.text or "").strip())
                    if u:
                        if ptag == "sitemap":
                            if u not in seen_maps and len(seen_maps) < 200:
                                seen_maps.add(u)
                                res.sitemaps.append(u)
                        elif u not in seen:
                            seen.add(u)
                            res.urls.append(u)
                    if len(res.urls) >= max_urls:
                        res.truncated = True
                        break
                if local in ("url", "sitemap"):
                    el.clear()
        except etree.XMLSyntaxError:
            pass
        if not res.urls and not res.sitemaps:  # recover-mode produced nothing: regex fallback
            for m in _LOC_RE.finditer(data.decode("utf-8", "replace")):
                u = normalise_url(m.group(1))
                if u and u not in seen:
                    seen.add(u)
                    res.urls.append(u)
                    if len(res.urls) >= max_urls:
                        res.truncated = True
                        break
    except Exception as e:  # never propagate parser errors
        res.error = f"{type(e).__name__}: {e}"[:200]
    return res


def pick_contact_urls(
    urls: Iterable[str],
    base_url: str,
    *,
    keywords: Iterable[str] = (),
    is_same_site: Callable[[str], bool] | None = None,
    limit: int = 10,
) -> list[RankedLink]:
    """Select the sitemap URLs most likely to hold contact details (contact/team/about/...)."""
    return rank_internal_links(urls, base_url, keywords=keywords, is_same_site=is_same_site, limit=limit)
