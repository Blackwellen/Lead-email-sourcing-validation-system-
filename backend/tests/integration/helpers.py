"""Shared pieces for the discovery integration tests: a host-aware port-80 fixture web server, patched DNS, run helpers.

Everything is synthetic and local; hostnames are remapped to 127.0.0.1 by an injected resolver. Nothing claims to be real data.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import text

from app import db as dbmod
from app import runs
from app.config import get_settings
from app.crawler import safe_http
from app.models import User
from app.pipeline import handlers, runtime
from app.run_config import RunConfig
from app.worker import Worker


class HostServer:
    """pages[host][path] = html | (status, body, content-type). ``hits`` records every request as (host, path)."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.hits: list[tuple[str, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                host = (self.headers.get("Host") or "").split(":")[0].lower().removeprefix("www.")
                path = self.path.split("?")[0]
                outer.hits.append((host, path))
                page = outer.pages.get(host, {}).get(path)
                status, ctype, body = 404, "text/html", b"<html><body>not found</body></html>"
                if page is not None:
                    if isinstance(page, tuple):
                        status, raw = page[0], page[1]
                        ctype = page[2] if len(page) > 2 else "text/html; charset=utf-8"
                    else:
                        status, raw = 200, page
                    body = raw if isinstance(raw, bytes) else raw.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.H = H
        self.httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 80), self.H)
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda *a: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def hits_for(self, host: str, path: str | None = None) -> int:
        return sum(1 for h, p in self.hits if h == host and (path is None or p == path))

    @property
    def hosts(self) -> set[str]:
        return set(self.pages)


def start_server() -> HostServer:
    srv = HostServer()
    try:
        srv.start()
    except OSError:
        pytest.skip("cannot bind 127.0.0.1:80")
    return srv


async def patch_network(monkeypatch, srv: HostServer, mail_domains: list[str] | None = None) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from verify.fakes import FakeResolver

    monkeypatch.setattr(get_settings(), "allow_private_networks", True)
    monkeypatch.setattr(runtime, "MIN_INTERVAL", 0.0)

    async def fake_resolver(host, port):
        h = host.lower().removeprefix("www.")
        return ["127.0.0.1"] if h in srv.hosts else []

    monkeypatch.setattr(safe_http, "system_resolver", fake_resolver)
    await runtime.close_net()
    runtime.reset_gates()
    dns = FakeResolver()
    for d in mail_domains or []:
        dns.mail_domain(d, [(10, f"mail.{d}")])
    monkeypatch.setattr(handlers, "VERIFY_RESOLVER", dns)


async def make_run(cfg: RunConfig, *, start: bool = True) -> int:
    async with dbmod.sessionmaker()() as s:
        u = (await s.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
        if u is None:
            usr = User(email="int@example.com", password_hash="x", role="admin")
            s.add(usr)
            await s.flush()
            u = usr.id
        run = await runs.create_run(s, cfg, user_id=u)
        if start:
            await runs.start_run(s, run.id)
        await s.commit()
        return run.id


async def run_until_done(run_id: int, *, timeout: float = 120, concurrency: int = 6, worker: Worker | None = None) -> str:
    w = worker or Worker(concurrency=concurrency)
    task = asyncio.create_task(w.run())
    end = asyncio.get_event_loop().time() + timeout
    status = ""
    try:
        while asyncio.get_event_loop().time() < end:
            async with dbmod.sessionmaker()() as s:
                status = (await s.execute(text("SELECT status FROM sourcing_runs WHERE id=:r"), {"r": run_id})).scalar()
            if status in ("completed", "failed"):
                break
            await asyncio.sleep(0.3)
    finally:
        w.request_stop()
        await task
        await runtime.close_net()
    return status


async def drain_jobs(run_id: int, stages: list[str] | None = None, timeout: float = 60) -> None:
    """Run a worker until no queued/leased job is left for the run (run stays as it is)."""
    w = Worker(concurrency=4, stages=stages)
    task = asyncio.create_task(w.run())
    end = asyncio.get_event_loop().time() + timeout
    try:
        while asyncio.get_event_loop().time() < end:
            async with dbmod.sessionmaker()() as s:
                n = (await s.execute(text("SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state IN ('queued','leased')"), {"r": run_id})).scalar()
            if n == 0:
                break
            await asyncio.sleep(0.3)
    finally:
        w.request_stop()
        await task
        await runtime.close_net()


async def q(sql: str, **p):
    async with dbmod.sessionmaker()() as s:
        return (await s.execute(text(sql), p)).all()


async def scalar(sql: str, **p):
    async with dbmod.sessionmaker()() as s:
        return (await s.execute(text(sql), p)).scalar()
