import asyncio
import time

import pytest

from app.crawler.ratelimit import RateLimiter, WaitTooLong, ip_group_key, parse_retry_after


def test_parse_retry_after():
    assert parse_retry_after("120") == 120
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    from datetime import UTC, datetime
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Thu, 01 Jan 2026 12:01:00 GMT", now) == 60
    assert parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now) == 0


def test_ip_group_key():
    assert ip_group_key("203.0.113.77") == "203.0.113.0/24"
    assert ip_group_key("2001:db8:1:2::5").startswith("2001:db8:1::/48")


async def test_min_interval_spacing_per_host():
    rl = RateLimiter(min_interval=0.1, per_host_concurrency=4)
    stamps = []

    async def worker():
        async with rl.acquire("a.test"):
            stamps.append(time.monotonic())

    await asyncio.gather(*[worker() for _ in range(4)])
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= 0.085 for g in gaps), gaps


async def test_different_hosts_do_not_wait_for_each_other():
    rl = RateLimiter(min_interval=0.3)
    t = time.monotonic()

    async def hit(h):
        async with rl.acquire(h):
            pass

    await asyncio.gather(*[hit(f"h{i}.test") for i in range(5)])
    assert time.monotonic() - t < 0.25


async def test_per_host_and_global_concurrency_caps():
    rl = RateLimiter(min_interval=0, per_host_concurrency=2, global_concurrency=3)
    cur = {"host": 0, "all": 0, "max_host": 0, "max_all": 0}

    async def work(host):
        async with rl.acquire(host):
            cur["host"] += 1
            cur["all"] += 1
            cur["max_host"] = max(cur["max_host"], cur["host"] if host == "a" else 0)
            cur["max_all"] = max(cur["max_all"], cur["all"])
            await asyncio.sleep(0.03)
            cur["all"] -= 1
            cur["host"] -= 1 if host == "a" else 0
            if host != "a":
                pass

    await asyncio.gather(*([work("a") for _ in range(6)] + [work(f"b{i}") for i in range(6)]))
    assert cur["max_host"] <= 2
    assert cur["max_all"] <= 3


async def test_backoff_on_429_honours_retry_after():
    t = {"now": 100.0}
    rl = RateLimiter(min_interval=0, clock=lambda: t["now"], base_backoff=2, max_backoff=60)
    async with rl.acquire("a.test") as slot:
        slot.report(429, retry_after=45)
    assert rl.backoff_remaining("a.test") == pytest.approx(45)
    with pytest.raises(WaitTooLong):
        async with rl.acquire("a.test", max_wait=5):
            pass
    t["now"] += 46
    assert rl.backoff_remaining("a.test") == 0
    async with rl.acquire("a.test", max_wait=5):
        pass


async def test_exponential_backoff_on_5xx_and_recovery():
    t = {"now": 0.0}
    rl = RateLimiter(min_interval=0, clock=lambda: t["now"], base_backoff=2, max_backoff=20)
    delays = []
    for _ in range(5):
        rl.report("h", 503)
        delays.append(round(rl.backoff_remaining("h")))
        t["now"] += rl.backoff_remaining("h") + 0.01
    assert delays == [2, 4, 8, 16, 20]  # doubles, capped
    rl.report("h", 200)
    rl.report("h", 503)
    assert round(rl.backoff_remaining("h")) == 2  # success reset the ladder


async def test_real_wait_during_backoff():
    rl = RateLimiter(min_interval=0, base_backoff=0.15)
    rl.report("h", 503)
    t = time.monotonic()
    async with rl.acquire("h"):
        pass
    assert time.monotonic() - t >= 0.12


async def test_crawl_delay_raises_interval():
    rl = RateLimiter(min_interval=0.01)
    rl.set_crawl_delay("h", 0.12)
    assert rl.interval_for("h") == 0.12
    t = time.monotonic()
    for _ in range(3):
        async with rl.acquire("h"):
            pass
    assert time.monotonic() - t >= 0.22


async def test_group_concurrency_across_hosts():
    rl = RateLimiter(min_interval=0, group_concurrency=1, per_host_concurrency=2)
    active = {"n": 0, "max": 0}

    async def work(host):
        async with rl.acquire(host, group="203.0.113.0/24"):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(0.02)
            active["n"] -= 1

    await asyncio.gather(*[work(f"site{i}.test") for i in range(5)])
    assert active["max"] == 1


async def test_eviction_bounds_memory():
    t = {"now": 0.0}
    rl = RateLimiter(min_interval=0, max_hosts=50, idle_evict_s=10, clock=lambda: t["now"])
    for i in range(200):
        async with rl.acquire(f"h{i}.test"):
            pass
        t["now"] += 1
    assert rl.tracked_hosts <= 51
    # a host in backoff is retained
    rl.report("keep.test", 429, 100)
    for i in range(200):
        async with rl.acquire(f"x{i}.test"):
            pass
        t["now"] += 0.1
    assert rl.backoff_remaining("keep.test") > 0


async def test_cancelled_waiters_release_capacity():
    rl = RateLimiter(min_interval=0, per_host_concurrency=1)
    gate = asyncio.Event()

    async def holder():
        async with rl.acquire("h"):
            await gate.wait()

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    w = asyncio.create_task(rl.acquire("h").__aenter__())
    await asyncio.sleep(0.01)
    w.cancel()
    gate.set()
    await h
    async with rl.acquire("h"):  # would deadlock if a cancelled waiter leaked the semaphore
        pass
