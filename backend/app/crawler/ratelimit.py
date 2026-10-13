"""Async politeness limiter: global + per-host (+ optional per-hosting-group) concurrency,
minimum request intervals, and host-level exponential backoff honouring Retry-After.

Memory is bounded: idle hosts/groups are evicted (LRU) once ``max_hosts`` is exceeded.
"""
from __future__ import annotations

import asyncio
import email.utils
import ipaddress
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

BACKOFF_STATUSES = frozenset({429, 500, 502, 503, 504})


class WaitTooLong(Exception):
    """The limiter would have to wait longer than the caller allows."""

    def __init__(self, wait: float, host: str) -> None:
        super().__init__(f"rate limit wait {wait:.1f}s for {host} exceeds allowance")
        self.wait = wait
        self.host = host


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date); None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(int(value))
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - (now or datetime.now(UTC))).total_seconds())


def ip_group_key(ip: str) -> str:
    """Hosting-provider grouping key: /24 for IPv4, /48 for IPv6."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


@dataclass(slots=True)
class _State:
    concurrency: int
    sem: asyncio.Semaphore = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_allowed: float = 0.0
    backoff_until: float = 0.0
    backoff_level: int = 0
    crawl_delay: float = 0.0
    last_used: float = 0.0
    active: int = 0
    waiters: int = 0
    consecutive_failures: int = 0

    def __post_init__(self) -> None:
        self.sem = asyncio.Semaphore(self.concurrency)


class Slot:
    """Handle yielded by ``RateLimiter.acquire``; call ``report`` with the response status."""

    __slots__ = ("_limiter", "host", "group")

    def __init__(self, limiter: RateLimiter, host: str, group: str | None) -> None:
        self._limiter, self.host, self.group = limiter, host, group

    def report(self, status: int | None, retry_after: float | None = None) -> None:
        self._limiter.report(self.host, status, retry_after)


class RateLimiter:
    def __init__(
        self,
        *,
        global_concurrency: int = 32,
        per_host_concurrency: int = 2,
        min_interval: float = 1.0,
        global_min_interval: float = 0.0,
        group_concurrency: int | None = None,
        group_min_interval: float = 0.0,
        base_backoff: float = 2.0,
        max_backoff: float = 300.0,
        max_retry_after: float = 900.0,
        max_hosts: int = 5000,
        idle_evict_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.global_concurrency = global_concurrency
        self.per_host_concurrency = per_host_concurrency
        self.min_interval = min_interval
        self.global_min_interval = global_min_interval
        self.group_concurrency = group_concurrency
        self.group_min_interval = group_min_interval
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_retry_after = max_retry_after
        self.max_hosts = max_hosts
        self.idle_evict_s = idle_evict_s
        self._clock = clock
        self._global = asyncio.Semaphore(global_concurrency)
        self._global_lock = asyncio.Lock()
        self._global_next = 0.0
        self._hosts: OrderedDict[str, _State] = OrderedDict()
        self._groups: OrderedDict[str, _State] = OrderedDict()

    # ------------------------------------------------------------------ state mgmt
    def _state(self, table: OrderedDict[str, _State], key: str, concurrency: int) -> _State:
        st = table.get(key)
        if st is None:
            st = table[key] = _State(concurrency)
            if len(table) > self.max_hosts:
                self._evict(table)
        else:
            table.move_to_end(key)
        st.last_used = self._clock()
        return st

    def _evict(self, table: OrderedDict[str, _State]) -> None:
        now = self._clock()
        for key in list(table):
            if len(table) <= self.max_hosts:
                break
            st = table[key]
            idle = st.active == 0 and st.waiters == 0 and not st.lock.locked()
            if idle and now >= st.backoff_until and now - st.last_used >= self.idle_evict_s:
                del table[key]
        # still over the cap and everything is recently used: drop the oldest idle ones anyway
        for key in list(table):
            if len(table) <= self.max_hosts:
                break
            st = table[key]
            if st.active == 0 and st.waiters == 0 and now >= st.backoff_until:
                del table[key]

    @property
    def tracked_hosts(self) -> int:
        return len(self._hosts)

    # ------------------------------------------------------------------ controls
    def set_crawl_delay(self, host: str, seconds: float | None) -> None:
        """Record robots.txt Crawl-delay: raises (never lowers) the host's minimum interval."""
        if seconds and seconds > 0:
            self._state(self._hosts, host, self.per_host_concurrency).crawl_delay = float(seconds)

    def interval_for(self, host: str) -> float:
        st = self._hosts.get(host)
        return max(self.min_interval, st.crawl_delay if st else 0.0)

    def backoff_remaining(self, host: str) -> float:
        st = self._hosts.get(host)
        return max(0.0, st.backoff_until - self._clock()) if st else 0.0

    def report(self, host: str, status: int | None, retry_after: float | None = None) -> None:
        """Feed back a response so the host can be backed off (429/5xx) or recovered."""
        st = self._state(self._hosts, host, self.per_host_concurrency)
        now = self._clock()
        failed = status is None or status in BACKOFF_STATUSES
        if failed:
            st.consecutive_failures += 1
            st.backoff_level = min(st.backoff_level + 1, 12)
            delay = min(self.max_backoff, self.base_backoff * (2 ** (st.backoff_level - 1)))
            if retry_after is not None:
                delay = max(delay if status != 429 else 0.0, min(retry_after, self.max_retry_after))
            st.backoff_until = max(st.backoff_until, now + delay)
        else:
            st.consecutive_failures = 0
            if st.backoff_level and now >= st.backoff_until:
                st.backoff_level = 0

    # ------------------------------------------------------------------ acquire
    @asynccontextmanager
    async def acquire(self, host: str, *, group: str | None = None, max_wait: float | None = None):
        """Wait for global/group/host capacity and the host's pacing slot.

        ``max_wait``: raise WaitTooLong instead of waiting longer than this for pacing/backoff.
        """
        hst = self._state(self._hosts, host, self.per_host_concurrency)
        grp = self._state(self._groups, group, self.group_concurrency or self.per_host_concurrency) if group else None
        hst.waiters += 1
        waiting = True
        got_global = got_group = got_host = False
        try:
            await self._global.acquire()
            got_global = True
            if grp is not None:
                await grp.sem.acquire()
                got_group = True
            await hst.sem.acquire()
            got_host = True
            hst.waiters -= 1
            waiting = False
            await self._pace(host, hst, grp, max_wait)
            hst.active += 1
            try:
                yield Slot(self, host, group)
            finally:
                hst.active -= 1
                hst.last_used = self._clock()
        finally:
            if waiting:
                hst.waiters -= 1
            if got_host:
                hst.sem.release()
            if got_group and grp is not None:
                grp.sem.release()
            if got_global:
                self._global.release()

    async def _pace(self, host: str, hst: _State, grp: _State | None, max_wait: float | None) -> None:
        interval = max(self.min_interval, hst.crawl_delay)
        while True:
            async with hst.lock:
                now = self._clock()
                start = max(now, hst.next_allowed, hst.backoff_until)
                if grp is not None and self.group_min_interval:
                    start = max(start, grp.next_allowed)
                if self.global_min_interval:
                    start = max(start, self._global_next)
                wait = start - now
                if max_wait is not None and wait > max_wait:
                    raise WaitTooLong(wait, host)
                hst.next_allowed = start + interval
                if grp is not None and self.group_min_interval:
                    grp.next_allowed = start + self.group_min_interval
                if self.global_min_interval:
                    self._global_next = start + self.global_min_interval
            if wait > 0:
                await asyncio.sleep(wait)
            # backoff may have been extended while we slept
            if self._clock() >= hst.backoff_until - 1e-3:
                return
            if max_wait is not None and hst.backoff_until - self._clock() > max_wait:
                raise WaitTooLong(hst.backoff_until - self._clock(), host)
