"""HTML helpers shared by graph / signals / changes (deterministic, never raise)."""
from __future__ import annotations

import re
from typing import Any

from ..crawler.extract import _loads_lenient

try:  # selectolax is a hard dependency of the crawler; keep import soft for tooling
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except Exception:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

_WS = re.compile(r"\s+")


def clean(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


def parse(html: str):
    if HTMLParser is None or not html:
        return None
    try:
        return HTMLParser(html)
    except Exception:
        return None


def _flatten(node: Any, out: list[dict], depth: int = 0) -> None:
    if depth > 8 or len(out) > 2000:
        return
    if isinstance(node, list):
        for x in node:
            _flatten(x, out, depth + 1)
    elif isinstance(node, dict):
        out.append(node)
        for k, v in node.items():
            if k == "@graph" or isinstance(v, dict | list):
                _flatten(v, out, depth + 1)


def jsonld_nodes(html: str, tree=None) -> list[dict]:
    """All JSON-LD objects on the page, flattened (``@graph`` and nested objects included)."""
    tree = tree if tree is not None else parse(html)
    out: list[dict] = []
    if tree is None:
        return out
    for n in tree.css('script[type="application/ld+json"]'):
        data = _loads_lenient(n.text() or "")
        if data is not None:
            _flatten(data, out)
    return out


def node_types(node: dict) -> list[str]:
    t = node.get("@type")
    ts = t if isinstance(t, list) else [t]
    return [str(x).rsplit("/", 1)[-1].lower() for x in ts if isinstance(x, str)]


def text_of(node, limit: int = 600) -> str:
    try:
        return clean(node.text(separator=" ", strip=True))[:limit]
    except Exception:
        return ""


def visible_text(html: str) -> str:
    """Whole-page visible text without scripts/styles/nav/boilerplate containers."""
    tree = parse(html)
    if tree is None:
        return ""
    try:
        tree.strip_tags(["script", "style", "noscript", "svg", "iframe", "template", "nav"])
        body = tree.body or tree.root
        return clean(body.text(separator=" ", strip=True)) if body is not None else ""
    except Exception:
        return ""
