"""Pure-ASGI middleware: request id, security headers, access log + metrics, rate limiting, maintenance mode."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .db import sessionmaker
from .logging_setup import mask_pii
from .security import SlidingWindowLimiter, client_ip
from .settings_store import maintenance_state

log = logging.getLogger("api")

HTTP_REQUESTS = Counter("lead_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram("lead_http_request_seconds", "HTTP request latency", ["route"], buckets=(0.01, 0.05, 0.25, 1, 4, 15))

_RID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_DOCS = ("/api/docs", "/api/redoc", "/api/openapi.json")


def _json_error(status: int, code: str, message: str, headers: list[tuple[bytes, bytes]] | None = None) -> tuple[Message, Message]:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *(headers or [])]
    return {"type": "http.response.start", "status": status, "headers": hdrs}, {"type": "http.response.body", "body": body}


class RequestContextMiddleware:
    """Request id, security headers, access log, Prometheus HTTP metrics."""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.hsts = settings.cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode("latin-1")
        if not _RID.match(rid):
            rid = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        path = scope.get("path", "")
        t0 = time.perf_counter()
        status_holder = {"s": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["s"] = message["status"]
                h = list(message.get("headers") or [])
                names = {k.lower() for k, _ in h}
                extra = {
                    b"x-request-id": rid.encode(),
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"cross-origin-resource-policy": b"same-site",
                }
                if not path.startswith(_DOCS):
                    extra[b"content-security-policy"] = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                if path.startswith("/api/") and b"cache-control" not in names:
                    extra[b"cache-control"] = b"no-store"
                if self.hsts:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                for k, v in extra.items():
                    if k not in names:
                        h.append((k, v))
                message = {**message, "headers": h}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur = time.perf_counter() - t0
            route = getattr(scope.get("route"), "path", None) or ("unmatched" if status_holder["s"] == 404 else path.split("?")[0][:40])
            HTTP_REQUESTS.labels(scope.get("method", "?"), route, str(status_holder["s"])).inc()
            HTTP_SECONDS.labels(route).observe(dur)
            if path not in ("/healthz", "/metrics"):
                log.info(mask_pii(f"{scope.get('method')} {path} -> {status_holder['s']} {dur * 1000:.1f}ms rid={rid}"))


class RateLimitMiddleware:
    """Per-client-IP sliding-window limiter for the whole API (in-memory; see SlidingWindowLimiter)."""

    def __init__(self, app: ASGIApp, limiter: SlidingWindowLimiter):
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in ("/healthz", "/readyz", "/metrics"):
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request

        ip = client_ip(Request(scope))
        ok, retry = self.limiter.check(ip)
        if not ok:
            start, body = _json_error(
                429, "rate_limited", "Too many requests. Please slow down and try again shortly.",
                [(b"retry-after", str(int(retry) + 1).encode())],
            )
            await send(start)
            await send(body)
            return
        await self.app(scope, receive, send)


class MaintenanceMiddleware:
    """While maintenance mode is on, mutating API calls (except /api/system and /api/auth) get 503."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            path = scope.get("path", "")
            if path.startswith("/api/") and not path.startswith(("/api/system", "/api/auth")):
                try:
                    async with sessionmaker()() as s:
                        st = await maintenance_state(s)
                except Exception:  # DB trouble must not mask the real error path
                    st = {"enabled": False}
                if st["enabled"]:
                    msg = st.get("message") or "The system is in maintenance mode; changes are temporarily disabled."
                    start, body = _json_error(503, "maintenance_mode", msg, [(b"retry-after", b"60")])
                    await send(start)
                    await send(body)
                    return
        await self.app(scope, receive, send)
