"""Polite JSON/HTTP helper for the API-based discovery adapters (pacing, 429/5xx back-off, ``Retry-After``).

Unlike the quota-bound search providers (which STOP on 429), the public open-data APIs used here answer with an
explicit "retry after N seconds" instruction (Contracts Finder: ``Rate limit of 12 exceeded. Please retry after
120 seconds.``).  Waiting exactly as instructed -- bounded by ``max_wait_s`` and ``max_attempts`` -- is honouring the
limit, not circumventing it.  Beyond the bound the run stops with :class:`RateLimited` so a worker can resume from
its checkpoint later.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .base import AdapterError
from .download import make_client

Sleep = Callable[[float], Awaitable[None]]
_RETRY_TEXT = re.compile(r"retry after\s+(\d+)\s*s", re.I)


class RateLimited(AdapterError):
    """The service kept answering 429/503 beyond our bounded back-off; resume later from the checkpoint."""


class HttpFailure(AdapterError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def parse_retry_after(resp: httpx.Response) -> float | None:
    ra = resp.headers.get("Retry-After")
    if ra:
        ra = ra.strip()
        if ra.isdigit():
            return float(ra)
        try:
            dt = parsedate_to_datetime(ra)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    try:
        m = _RETRY_TEXT.search(resp.text[:500])
    except Exception:  # noqa: BLE001 - body may be undecodable
        m = None
    return float(m.group(1)) if m else None


@dataclass
class PoliteClient:
    """``httpx`` client wrapper: min interval between requests, bounded retries on 429/5xx."""

    client: httpx.AsyncClient | None = None
    min_interval_s: float = 1.0
    max_attempts: int = 4
    backoff_s: float = 2.0
    max_wait_s: float = 180.0
    sleep: Sleep = asyncio.sleep
    retry_statuses: tuple[int, ...] = (500, 502, 503, 504)
    headers: dict[str, str] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=lambda: {"requests": 0, "retries": 0, "rate_limited": 0})
    _last: float = 0.0
    _own: bool = False

    async def __aenter__(self) -> PoliteClient:
        if self.client is None:
            self.client = make_client()
            self._own = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._own and self.client is not None:
            await self.client.aclose()

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        assert self.client is not None, "use 'async with PoliteClient(...)'"
        hdrs = {**self.headers, **(kw.pop("headers", None) or {})}
        last_status = 0
        for attempt in range(1, self.max_attempts + 1):
            gap = self.min_interval_s - (time.monotonic() - self._last)
            if gap > 0 and self._last:
                await self.sleep(gap)
            self._last = time.monotonic()
            self.stats["requests"] += 1
            try:
                resp = await self.client.request(method, url, headers=hdrs, **kw)
            except httpx.TransportError as exc:
                if attempt >= self.max_attempts:
                    raise HttpFailure(0, f"transport error after {attempt} attempts: {exc}") from exc
                self.stats["retries"] += 1
                await self.sleep(min(self.max_wait_s, self.backoff_s * 2 ** (attempt - 1)))
                continue
            last_status = resp.status_code
            if resp.status_code == 429 or (resp.status_code == 503 and parse_retry_after(resp) is not None):
                self.stats["rate_limited"] += 1
                wait = parse_retry_after(resp)
                if wait is None:
                    wait = self.backoff_s * 2 ** (attempt - 1)
                if wait > self.max_wait_s or attempt >= self.max_attempts:
                    raise RateLimited(f"HTTP {resp.status_code} from {url.split('?')[0]}; asked to wait {wait:.0f}s; stopping (resume from checkpoint)")
                self.stats["retries"] += 1
                await self.sleep(wait)
                continue
            if resp.status_code in self.retry_statuses:
                if attempt >= self.max_attempts:
                    break
                self.stats["retries"] += 1
                await self.sleep(min(self.max_wait_s, parse_retry_after(resp) or self.backoff_s * 2 ** (attempt - 1)))
                continue
            return resp
        raise HttpFailure(last_status, f"HTTP {last_status} from {url.split('?')[0]} after {self.max_attempts} attempts")

    async def get_json(self, url: str, **kw: Any) -> Any:
        resp = await self.request("GET", url, **kw)
        if resp.status_code >= 400:
            raise HttpFailure(resp.status_code, f"HTTP {resp.status_code} from {url.split('?')[0]}")
        return resp.json()

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        resp = await self.request("POST", url, json=body, **kw)
        if resp.status_code >= 400:
            raise HttpFailure(resp.status_code, f"HTTP {resp.status_code} from {url.split('?')[0]}: {resp.text[:200]}")
        return resp.json()
