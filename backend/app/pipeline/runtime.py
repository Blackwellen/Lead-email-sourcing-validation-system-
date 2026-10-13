"""Per-process shared network primitives (one pooled client, one limiter, one robots cache)."""
from __future__ import annotations

import asyncio

from ..config import get_settings
from ..crawler.ratelimit import RateLimiter
from ..crawler.robots import RobotsCache
from ..crawler.safe_http import DbCacheStore, SafeFetcher

MIN_INTERVAL = 1.0  # polite default per-host spacing (seconds); tests lower it
_state: dict[str, object] = {}
_lock = asyncio.Lock()


async def get_net(per_host: int = 1, global_concurrency: int = 32, min_interval: float | None = None):
    """Returns (fetcher, limiter, robots). Created lazily inside the running loop."""
    async with _lock:
        if "fetcher" not in _state:
            f = SafeFetcher(get_settings(), cache=DbCacheStore())
            _state["fetcher"] = f
            _state["limiter"] = RateLimiter(global_concurrency=global_concurrency, per_host_concurrency=per_host, min_interval=MIN_INTERVAL if min_interval is None else min_interval)
            _state["robots"] = RobotsCache(f)
    return _state["fetcher"], _state["limiter"], _state["robots"]


async def close_net() -> None:
    f = _state.pop("fetcher", None)
    _state.pop("limiter", None)
    _state.pop("robots", None)
    if f is not None:
        await f.aclose()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- per-source gates (rate policy + concurrency)
_gates: dict[str, object] = {}


def adapter_gate(info):
    """Process-wide :class:`~app.discovery.scheduler.SourceBudget` for one adapter, built from its ``AdapterInfo.rate_policy``.

    ``try_acquire`` takes one request token + one concurrency slot (sliding 60 s window AND token bucket: never more than
    ``requests_per_minute`` grants in any 60 s span); ``release`` frees the slot. Quotas are enforced separately from measured
    counters (``flow.QuotaGuard``) so they survive restarts."""
    from ..discovery.scheduler import SourceBudget

    g = _gates.get(info.adapter_id)
    if g is None:
        rp = info.rate_policy
        g = _gates[info.adapter_id] = SourceBudget(info.adapter_id, rp.requests_per_minute, rp.concurrency)
    return g


def reset_gates() -> None:
    _gates.clear()
