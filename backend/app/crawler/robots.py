"""robots.txt (RFC 9309) parsing, caching and policy.

* 2xx           -> parse and obey
* 4xx (not 429) -> robots.txt "unavailable": everything allowed
* 5xx / 429 / unreachable -> "unavailable-for-now": crawling is DEFERRED (treated as disallowed
  until a later check succeeds). We never assume permission from a server error.
* ``Crawl-delay`` is exposed so the rate limiter can honour it.
"""
from __future__ import annotations

import asyncio
import enum
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from .safe_http import ROBOTS_TYPES, SafeFetcher

MAX_ROBOTS_BYTES = 512 * 1024
_MAX_RULES = 5000


class Decision(enum.StrEnum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    DEFERRED = "deferred"  # robots.txt unreachable / 5xx: try again later


@dataclass(slots=True)
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow?, pattern)
    crawl_delay: float | None = None


@dataclass(slots=True)
class RobotsRules:
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)

    def group_for(self, agent: str) -> Group | None:
        """Most specific matching group(s) merged (RFC 9309 2.2.1), falling back to ``*``."""
        agent = agent.lower()
        best_len = -1
        matched: list[Group] = []
        for g in self.groups:
            for a in g.agents:
                if a == "*":
                    continue
                if a in agent:
                    if len(a) > best_len:
                        best_len, matched = len(a), [g]
                    elif len(a) == best_len and g not in matched:
                        matched.append(g)
        if not matched:
            matched = [g for g in self.groups if "*" in g.agents]
        if not matched:
            return None
        merged = Group(agents=[agent])
        for g in matched:
            merged.rules.extend(g.rules)
            if g.crawl_delay is not None and merged.crawl_delay is None:
                merged.crawl_delay = g.crawl_delay
        return merged

    def is_allowed(self, agent: str, path_and_query: str) -> bool:
        g = self.group_for(agent)
        if g is None:
            return True
        path = _norm_pct(path_and_query or "/")
        best: tuple[int, bool] | None = None
        for allow, pat in g.rules:
            if not pat:
                continue
            if _compile(pat).match(path):
                key = (len(pat), allow)
                if best is None or key[0] > best[0] or (key[0] == best[0] and allow and not best[1]):
                    best = key
        return True if best is None else best[1]

    def crawl_delay(self, agent: str) -> float | None:
        g = self.group_for(agent)
        return g.crawl_delay if g else None


_PCT = re.compile(r"%([0-9a-fA-F]{2})")
_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _norm_pct(s: str) -> str:
    def rep(m: re.Match[str]) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    return _PCT.sub(rep, s)


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern[str]:
    pat = _norm_pct(pattern)
    anchored = pat.endswith("$")
    if anchored:
        pat = pat[:-1]
    rx = ".*".join(re.escape(p) for p in pat.split("*"))
    return re.compile(rx + ("$" if anchored else ""))


def parse_robots(text: str) -> RobotsRules:
    rules = RobotsRules()
    text = text.lstrip("﻿")
    cur: Group | None = None
    last_was_agent = False
    n_rules = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in ("user-agent", "useragent"):
            if cur is None or not last_was_agent:
                cur = Group()
                rules.groups.append(cur)
            if value:
                cur.agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if key == "sitemap":
            if value and len(rules.sitemaps) < 50:
                rules.sitemaps.append(value)
        elif cur is None:
            continue
        elif key in ("allow", "disallow") and n_rules < _MAX_RULES:
            cur.rules.append((key == "allow", value))
            n_rules += 1
        elif key == "crawl-delay":
            try:
                d = float(value)
                if d >= 0 and cur.crawl_delay is None:
                    cur.crawl_delay = d
            except ValueError:
                pass
    return rules


@dataclass(slots=True)
class RobotsCheck:
    decision: Decision
    crawl_delay: float | None = None
    reason: str = ""
    error_code: str | None = None  # transport-level failure code when robots.txt could not be fetched

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOWED


@dataclass(slots=True)
class _Entry:
    rules: RobotsRules | None  # None = allow-all (unavailable) or deferred
    deferred: bool
    expires: float
    status: int | None = None
    reason: str = ""
    error_code: str | None = None


class RobotsCache:
    def __init__(
        self,
        fetcher: SafeFetcher,
        *,
        ttl: float = 3600.0,
        deferred_ttl: float = 300.0,
        max_hosts: int = 2000,
        user_agent_token: str | None = None,
        limiter=None,
        clock=time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self.ttl = ttl
        self.deferred_ttl = deferred_ttl
        self.max_hosts = max_hosts
        self.agent = (user_agent_token or fetcher.robots_token).lower()
        self._limiter = limiter
        self._clock = clock
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_Entry]] = {}

    @staticmethod
    def origin_of(url: str) -> str | None:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        host = p.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        return f"{p.scheme}://{host}" + (f":{p.port}" if p.port else "")

    async def _entry(self, origin: str) -> _Entry:
        now = self._clock()
        e = self._cache.get(origin)
        if e and e.expires > now:
            self._cache.move_to_end(origin)
            return e
        fut = self._inflight.get(origin)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[origin] = fut
        try:
            e = await self._load(origin)
            self._cache[origin] = e
            self._cache.move_to_end(origin)
            while len(self._cache) > self.max_hosts:
                self._cache.popitem(last=False)
            fut.set_result(e)
            return e
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(origin, None)

    async def _load(self, origin: str) -> _Entry:
        url = origin + "/robots.txt"
        host = urlsplit(origin).hostname or origin
        if self._limiter is not None:
            async with self._limiter.acquire(host) as slot:
                res = await self._fetcher.fetch(url, accept=ROBOTS_TYPES, max_bytes=MAX_ROBOTS_BYTES, conditional=False)
                slot.report(res.status, res.retry_after() if res.status in (429, 503) else None)
        else:
            res = await self._fetcher.fetch(url, accept=ROBOTS_TYPES, max_bytes=MAX_ROBOTS_BYTES, conditional=False)
        now = self._clock()
        if res.error_code and res.error_code not in ("too_large", "bad_content_type"):
            return _Entry(None, True, now + self.deferred_ttl, res.status, f"robots unreachable: {res.error_code}",
                          res.error_code)
        st = res.status or 0
        if st == 429 or st >= 500:
            return _Entry(None, True, now + self.deferred_ttl, st, f"robots.txt returned {st}")
        if 200 <= st < 300:
            if res.error_code == "bad_content_type":
                return _Entry(None, False, now + self.ttl, st, "robots.txt has unusable content type; allow-all")
            return _Entry(parse_robots(res.text()), False, now + self.ttl, st, "parsed")
        return _Entry(None, False, now + self.ttl, st, f"robots.txt unavailable ({st}); allow-all")

    async def check(self, url: str) -> RobotsCheck:
        origin = self.origin_of(url)
        if origin is None:
            return RobotsCheck(Decision.DISALLOWED, reason="not an http(s) url")
        e = await self._entry(origin)
        if e.deferred:
            return RobotsCheck(Decision.DEFERRED, reason=e.reason, error_code=e.error_code)
        if e.rules is None:
            return RobotsCheck(Decision.ALLOWED, reason=e.reason)
        p = urlsplit(url)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        delay = e.rules.crawl_delay(self.agent)
        if e.rules.is_allowed(self.agent, path):
            return RobotsCheck(Decision.ALLOWED, delay, "allowed by robots.txt")
        return RobotsCheck(Decision.DISALLOWED, delay, "disallowed by robots.txt")

    async def can_fetch(self, url: str) -> bool:
        return (await self.check(url)).allowed

    async def sitemaps(self, url: str) -> list[str]:
        origin = self.origin_of(url)
        if origin is None:
            return []
        e = await self._entry(origin)
        return list(e.rules.sitemaps) if e.rules else []

    def invalidate(self, url: str) -> None:
        origin = self.origin_of(url)
        if origin:
            self._cache.pop(origin, None)
