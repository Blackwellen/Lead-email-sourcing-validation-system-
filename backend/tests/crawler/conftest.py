"""Local fixture HTTP server (127.0.0.1, ephemeral port) for crawler tests. No real internet."""
from __future__ import annotations

import gzip
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import get_settings
from app.crawler.safe_http import SafeFetcher


@dataclass
class Route:
    body: bytes | Callable[[BaseHTTPRequestHandler], bytes] = b""
    status: int = 200
    ctype: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    gzip: bool = False
    chunked_forever: bool = False


class FixtureServer:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def do_GET(self):  # noqa: N802
                path = self.path
                outer.requests.append((path, {k.lower(): v for k, v in self.headers.items()}))
                r = outer.routes.get(path) or outer.routes.get(path.split("?")[0])
                if r is None:
                    r = Route(b"<html><body>not found</body></html>", 404)
                body = r.body(self) if callable(r.body) else r.body
                if r.gzip:
                    body = gzip.compress(body)
                self.send_response(r.status)
                self.send_header("Content-Type", r.ctype)
                for k, v in r.headers.items():
                    self.send_header(k, v)
                if r.gzip:
                    self.send_header("Content-Encoding", "gzip")
                if r.chunked_forever:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for _ in range(100000):
                            chunk = b"x" * 8192
                            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            time.sleep(0.0005)
                    except OSError:
                        pass
                    return
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def add(self, path: str, body: bytes | str = b"", status: int = 200, ctype: str = "text/html; charset=utf-8",
            headers: dict[str, str] | None = None, gzip_: bool = False, **kw) -> Route:
        if isinstance(body, str):
            body = body.encode()
        r = Route(body, status, ctype, headers or {}, gzip_, **kw)
        self.routes[path] = r
        return r

    def hits(self, path: str) -> int:
        return sum(1 for p, _ in self.requests if p.split("?")[0] == path)

    def start(self) -> FixtureServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FixtureServer().start()
    yield s
    s.stop()


@pytest.fixture
def allow_private(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_private_networks", True)


@pytest.fixture
async def fetcher(allow_private):
    f = SafeFetcher()
    yield f
    await f.aclose()
