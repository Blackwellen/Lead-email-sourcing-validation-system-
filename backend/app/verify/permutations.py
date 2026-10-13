"""Candidate address generation (spec section 7) and pattern inference.

Everything produced here is **speculative**: origin ``pattern_inferred`` (named) or ``role_speculative``
(role mailbox). A generated address is never ``published`` and MX existence alone never makes it
high-confidence (see ``scoring``: speculative origins are capped and never 'safe'). Generate few
candidates, only for domains you are permitted to work with, and only verify them under the SMTP rules.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PATTERNS: dict[str, Callable[[str, str], str]] = {
    "first": lambda f, last: f,
    "last": lambda f, last: last,
    "first.last": lambda f, last: f"{f}.{last}",
    "first_last": lambda f, last: f"{f}_{last}",
    "first-last": lambda f, last: f"{f}-{last}",
    "flast": lambda f, last: f"{f[:1]}{last}",
    "f.last": lambda f, last: f"{f[:1]}.{last}",
    "firstlast": lambda f, last: f"{f}{last}",
    "last.first": lambda f, last: f"{last}.{f}",
    "lastf": lambda f, last: f"{last}{f[:1]}",
    "firstl": lambda f, last: f"{f}{last[:1]}",
}
# Rough prior order of prevalence in business mail (used only when no evidence exists).
DEFAULT_PATTERN_ORDER = ("first.last", "first", "flast", "firstlast", "f.last", "firstl", "first_last", "first-last", "last", "last.first", "lastf")
DEFAULT_NAMED_LIMIT = 3
HARD_NAMED_LIMIT = 15
HARD_ROLE_LIMIT = 10

_SPECIAL = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "þ": "th", "ð": "d", "ı": "i"})
_PARTICLES = frozenset({"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "la", "le", "ter", "ten", "bin", "ibn", "al", "el", "st", "mac", "mc"})

ROLE_BASE = ("info", "contact", "hello", "enquiries", "office", "sales", "admin")
ROLE_BY_INDUSTRY: dict[str, tuple[str, ...]] = {
    "hospitality": ("reservations", "bookings", "events"),
    "hotel": ("reservations", "bookings", "reception"),
    "restaurant": ("reservations", "bookings"),
    "travel": ("bookings", "reservations"),
    "recruitment": ("recruitment", "careers", "jobs"),
    "wholesale": ("suppliers", "trade", "orders"),
    "manufacturing": ("suppliers", "orders", "operations"),
    "retail": ("support", "orders", "customerservice"),
    "professional": ("accounts", "enquiries", "reception"),
    "health": ("reception", "appointments", "bookings"),
    "property": ("lettings", "reception", "sales"),
    "technology": ("support", "partnerships", "sales"),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    email: str
    local_part: str
    domain: str
    pattern: str
    origin: str  # pattern_inferred | role_speculative -- NEVER published
    confidence_hint: float  # 0..0.5 at most; a prior, not a verification result
    note: str = ""


@dataclass(slots=True)
class PatternHypothesis:
    pattern: str
    sample_size: int
    supporting: int
    confidence: float
    conflicts: int = 0
    examples: list[str] = field(default_factory=list)


def to_ascii(s: str) -> str:
    """Transliterate to lowercase ASCII letters/digits (NFKD + a few special letters); drops everything else."""
    s = (s or "").translate(_SPECIAL)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def family_variants(family: str) -> list[str]:
    """Compound-surname spellings in order of likelihood: joined, hyphenated, final token only."""
    tokens = [t for t in re.split(r"[\s\-]+", (family or "").replace("'", "").replace("’", "").strip()) if t]
    tokens_a = [to_ascii(t) for t in tokens if to_ascii(t)]
    if not tokens_a:
        return []
    out = ["".join(tokens_a)]
    if "-" in (family or "") and len(tokens_a) > 1:
        out.append("-".join(tokens_a))
    if len(tokens_a) > 1:
        last_tok = tokens_a[-1]
        if len(tokens) > 1 and (tokens[0].lower() in _PARTICLES or "-" in family):
            out.append(last_tok)
    seen: list[str] = []
    for v in out:
        if v and v not in seen:
            seen.append(v)
    return seen


def _first_name(given: str) -> str:
    tok = re.split(r"[\s]+", (given or "").strip())[0] if (given or "").strip() else ""
    return to_ascii(tok)


def _valid_local(local: str) -> bool:
    return 1 <= len(local) <= 64 and not local.startswith((".", "-")) and not local.endswith((".", "-"))


def generate_named_candidates(
    given: str, family: str, domain: str, patterns: Sequence[str] | None = None, limit: int = DEFAULT_NAMED_LIMIT,
) -> list[Candidate]:
    """Speculative named addresses for an *evidenced* person. All have origin ``pattern_inferred``.

    ``patterns`` should come from :func:`infer_patterns` on published addresses at the same domain; with no
    evidence only the few most common patterns are produced (conservative caps).
    """
    f = _first_name(given)
    fams = family_variants(family)
    domain = (domain or "").strip().lower()
    if not f or not fams or not domain:
        return []
    limit = max(0, min(limit, HARD_NAMED_LIMIT))
    order = [p for p in (patterns or DEFAULT_PATTERN_ORDER) if p in PATTERNS]
    evidenced = bool(patterns)
    out: list[Candidate] = []
    seen: set[str] = set()
    for rank, pat in enumerate(order):
        for vi, fam in enumerate(fams):
            local = PATTERNS[pat](f, fam)
            if not _valid_local(local) or local in seen:
                continue
            # single-letter local parts are far too ambiguous
            if len(local) < 2:
                continue
            seen.add(local)
            hint = (0.45 if evidenced else 0.25) - 0.03 * rank - 0.05 * vi
            out.append(Candidate(f"{local}@{domain}", local, domain, pat, "pattern_inferred", round(max(0.05, min(0.5, hint)), 2),
                                 "pattern evidenced by published addresses" if evidenced else "generic prior; no pattern evidence"))
            if len(out) >= limit:
                return out
    return out


def generate_role_candidates(
    domain: str, industry: str | None = None, evidence: Mapping[str, Any] | None = None, limit: int = 5,
) -> list[Candidate]:
    """Speculative role mailboxes (info@, sales@ ...). Origin ``role_speculative``.

    ``evidence`` may contain ``published_locals`` (already published addresses to skip) and
    ``role_locals`` (role names seen on this organisation's own pages, which are promoted).
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return []
    ev = evidence or {}
    skip = {str(x).lower() for x in ev.get("published_locals", [])}
    promoted = [str(x).lower() for x in ev.get("role_locals", [])]
    ind = (industry or "").lower()
    extra: tuple[str, ...] = ()
    for key, roles in ROLE_BY_INDUSTRY.items():
        if key in ind:
            extra = roles
            break
    order: list[str] = []
    for r in [*promoted, *ROLE_BASE[:3], *extra, *ROLE_BASE[3:]]:
        if r not in order and r not in skip:
            order.append(r)
    limit = max(0, min(limit, HARD_ROLE_LIMIT))
    return [
        Candidate(f"{r}@{domain}", r, domain, "role", "role_speculative", round(max(0.05, 0.25 - 0.02 * i), 2), "role mailbox guess; nothing publishes it")
        for i, r in enumerate(order[:limit])
    ]


def _pair_fields(p: Any) -> tuple[str, str, str]:
    if isinstance(p, Mapping):
        return p.get("given") or p.get("given_name") or "", p.get("family") or p.get("family_name") or "", p.get("local") or p.get("local_part") or p.get("email", "").split("@")[0]
    given, family, local = p[0], p[1], p[2]
    return given, family, local.split("@")[0] if "@" in local else local


def matching_patterns(given: str, family: str, local: str) -> list[str]:
    """Which of the 11 patterns explain ``local`` for this person."""
    f = _first_name(given)
    local = local.lower().split("+", 1)[0]
    if not f:
        return []
    hits = []
    for pat, fn in PATTERNS.items():
        for fam in family_variants(family):
            if fn(f, fam) == local:
                hits.append(pat)
                break
    # 'first' / 'last' single-token matches are ambiguous with short names; keep but they will carry low sample counts
    return hits


def infer_patterns(known_pairs: Iterable[Any]) -> list[PatternHypothesis]:
    """Hypothesise a domain's naming convention from published ``(given, family, local_part)`` pairs.

    Confidence is deliberately conservative (``supporting / (sample + 2)``, capped 0.9): one example
    yields at most ~0.33. Only pairs where the person's name is evidenced should be supplied.
    """
    usable: list[tuple[str, list[str]]] = []
    for p in known_pairs:
        g, fam, local = _pair_fields(p)
        hits = matching_patterns(g, fam, local)
        if hits:
            usable.append((local, hits))
    n = len(usable)
    out: list[PatternHypothesis] = []
    for pat in PATTERNS:
        sup = [loc for loc, hits in usable if pat in hits]
        if not sup:
            continue
        conflicts = n - len(sup)
        conf = min(0.9, len(sup) / (n + 2))
        out.append(PatternHypothesis(pat, n, len(sup), round(conf, 3), conflicts, sup[:3]))
    out.sort(key=lambda h: (-h.confidence, DEFAULT_PATTERN_ORDER.index(h.pattern)))
    return out
