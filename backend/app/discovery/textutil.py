"""Small deterministic text helpers shared by the discovery engines (pure Python, no I/O)."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime

_NON_ALNUM = re.compile(r"[^0-9a-z]+")

STOPWORDS = frozenset(
    "a an and are as at be by for from in is it of on or the to with we our your us uk ltd limited llp plc "
    "co company services service group holdings solutions the&".split()
)


def fold(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def tokens(text: str | None, *, drop_stop: bool = True, min_len: int = 2) -> list[str]:
    s = fold(text).replace("&", " and ").replace("'", "")
    out = [t for t in _NON_ALNUM.split(s) if len(t) >= min_len]
    if drop_stop:
        out = [t for t in out if t not in STOPWORDS]
    return out


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(0, max(la, lb) // 2 - 1)
    ma = [False] * la
    mb = [False] * lb
    matches = 0
    for i, ca in enumerate(a):
        lo, hi = max(0, i - window), min(lb, i + window + 1)
        for j in range(lo, hi):
            if not mb[j] and b[j] == ca:
                ma[i] = mb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    t = 0
    k = 0
    for i in range(la):
        if ma[i]:
            while not mb[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    t //= 2
    return (matches / la + matches / lb + (matches - t) / matches) / 3.0


def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    j = jaro(a, b)
    if j <= 0.7:
        return j
    prefix = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb or prefix == 4:
            break
        prefix += 1
    return j + prefix * p * (1.0 - j)


def trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """pg_trgm-compatible set similarity: |A∩B| / |A∪B| over padded trigrams."""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def name_similarity(a: str, b: str) -> float:
    """Blend of Jaro-Winkler, trigram and token-set overlap on already-normalised names (0..1)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    tok = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return round(0.4 * jaro_winkler(a, b) + 0.35 * trigram_similarity(a, b) + 0.25 * tok, 4)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def tfidf_vectors(docs: dict[object, list[str]]) -> dict[object, dict[str, float]]:
    """Deterministic smoothed TF-IDF (sublinear tf, idf = ln((N+1)/(df+1)) + 1), L2-normalised."""
    n = len(docs)
    df: Counter[str] = Counter()
    for toks in docs.values():
        df.update(set(toks))
    out: dict[object, dict[str, float]] = {}
    for k, toks in docs.items():
        tf = Counter(toks)
        vec = {t: (1.0 + math.log(c)) * (math.log((n + 1) / (df[t] + 1)) + 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        out[k] = {t: v / norm for t, v in vec.items()}
    return out


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def utcnow() -> datetime:
    return datetime.now(UTC)


def uniq(items: Iterable) -> list:
    return list(dict.fromkeys(items))
