import gzip
import ssl
import time

import httpx
import pytest

from app.config import get_settings
from app.crawler import safe_http as sh
from app.crawler.safe_http import (
    DbCacheStore,
    DnsFailure,
    Guard,
    MemoryCacheStore,
    PinnedBackend,
    SafeFetcher,
    SSRFBlocked,
    classify_ip,
    decode_body,
)

PUBLIC = "93.184.216.34"


async def public_resolver(host, port):
    return [PUBLIC]


def strict_fetcher(**kw):
    kw.setdefault("resolver", public_resolver)
    return SafeFetcher(allow_private_networks=False, **kw)


BLOCKED_URLS = [
    "http://127.0.0.1/", "http://127.0.0.1:8080/admin", "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.5.4/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/",
    "http://[fd00:ec2::254]/", "http://[fe80::1]/", "http://[fc00::1]/", "http://100.64.0.1/", "http://0.0.0.0/",
    "http://224.0.0.1/", "http://240.0.0.1/", "http://198.18.0.1/", "http://100.100.100.200/",
    "http://2130706433/", "http://0x7f000001/", "http://0x7f.0.0.1/", "http://017700000001/", "http://127.1/",
    "http://0177.0.0.1/", "http://localhost/", "http://foo.localhost/", "http://127.0.0.1.:80/",
    "http://user:pw@example.com/", "http://evil.com@127.0.0.1/", "http://example.com@169.254.169.254/",
    "file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO", "ftp://example.com/", "javascript:alert(1)", "dict://x/",
    "http://example.com:22/", "http://example.com:6379/", "http://example.com:25/", "https://example.com:3306/",
    "http://%31%32%37.0.0.1/",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_ssrf_matrix_blocked_without_network(url):
    f = strict_fetcher()
    try:
        r = await f.fetch(url)
    finally:
        await f.aclose()
    assert r.error_code == "blocked_ssrf", (url, r.error_code, r.error_detail)
    assert r.status is None and r.body == b""


@pytest.mark.parametrize("url", ["", "http://", "http:///x", "http://exa mple.com/", "http://a\\b/", "http://example.com:99999/"])
async def test_invalid_urls_are_typed_not_raised(url):
    f = strict_fetcher()
    r = await f.fetch(url)
    await f.aclose()
    assert r.error_code in ("invalid_url", "blocked_ssrf")


def test_classify_ip():
    for bad in ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:10.0.0.1", "fd00:ec2::254", "100.64.0.1",
                "224.0.0.5", "192.0.2.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "fe80::1%eth0"]:
        assert classify_ip(bad), bad
    for good in ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"]:
        assert classify_ip(good) is None, good


async def test_hostname_resolving_to_private_blocked_any_address():
    async def mixed(host, port):
        return [PUBLIC, "10.0.0.7"]

    f = strict_fetcher(resolver=mixed)
    r = await f.fetch("http://sneaky.example.com/")
    await f.aclose()
    assert r.error_code == "blocked_ssrf" and "10.0.0.7" in (r.error_detail or "")


async def test_dns_failure_typed():
    async def nx(host, port):
        raise DnsFailure("nxdomain")

    f = strict_fetcher(resolver=nx)
    r = await f.fetch("http://nonexistent.example/")
    await f.aclose()
    assert r.error_code == "dns_failure"


async def test_dns_rebinding_blocked_at_connect_time():
    """Pre-flight sees a public IP, the connector's own lookup sees 127.0.0.1 -> refused, no connect."""
    calls = []

    async def rebinding(host, port):
        calls.append(host)
        return [PUBLIC] if len(calls) == 1 else ["127.0.0.1"]

    f = SafeFetcher(resolver=rebinding, allow_private_networks=False)  # real (pinned) transport
    r = await f.fetch("http://rebind.example.com/")
    await f.aclose()
    assert r.error_code == "blocked_ssrf"
    assert len(calls) >= 2


async def test_pinned_backend_validates_and_connects_to_ip(server, allow_private):
    seen = []

    async def resolver(host, port):
        seen.append(host)
        return ["127.0.0.1"]

    # hostname that does not exist in DNS is reached through the validated IP (pinning)
    f = SafeFetcher(resolver=resolver, allowed_ports={server.port})
    server.add("/", "<html>ok</html>")
    r = await f.fetch(f"http://pinned.test:{server.port}/")
    await f.aclose()
    assert r.ok and b"ok" in r.body and "pinned.test" in seen
    assert server.requests[-1][1]["host"] == f"pinned.test:{server.port}"  # Host header preserved


async def test_backend_rejects_private_when_not_allowed():
    g = Guard(resolver=public_resolver, allow_private=False)

    async def priv(host, port):
        return ["10.0.0.1"]

    g.resolver = priv
    with pytest.raises(SSRFBlocked):
        await PinnedBackend(g).connect_tcp("x.example", 80)


def mock_fetcher(handler, **kw):
    return strict_fetcher(transport=httpx.MockTransport(handler), **kw)


async def test_redirect_to_internal_blocked():
    def handler(req):
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    f = mock_fetcher(handler)
    r = await f.fetch("http://public.example/")
    await f.aclose()
    assert r.error_code == "blocked_ssrf" and r.redirects == ["http://public.example/"]


async def test_redirect_to_scheme_file_and_userinfo_blocked():
    for loc in ("file:///etc/passwd", "http://u:p@public.example/x", "gopher://x/"):
        f = mock_fetcher(lambda req, loc=loc: httpx.Response(301, headers={"Location": loc}))
        r = await f.fetch("http://public.example/")
        await f.aclose()
        assert r.error_code == "blocked_ssrf", loc


async def test_redirect_hostname_rebinding_resolves_private_blocked():
    async def resolver(host, port):
        return ["10.9.9.9"] if host == "internal.corp" else [PUBLIC]

    def handler(req):
        return httpx.Response(302, headers={"Location": "http://internal.corp/secret"})

    f = mock_fetcher(handler, resolver=resolver)
    r = await f.fetch("http://public.example/")
    await f.aclose()
    assert r.error_code == "blocked_ssrf"


async def test_redirect_loop_and_limit():
    def loop(req):
        return httpx.Response(302, headers={"Location": "/b" if req.url.path == "/a" else "/a"})

    f = mock_fetcher(loop)
    r = await f.fetch("http://public.example/a")
    assert r.error_code == "redirect_loop" and "loop" in r.error_detail

    n = {"i": 0}

    def long_chain(req):
        n["i"] += 1
        return httpx.Response(302, headers={"Location": f"/p{n['i']}"})

    f2 = mock_fetcher(long_chain, max_redirects=3)
    r2 = await f2.fetch("http://public.example/start")
    await f.aclose()
    await f2.aclose()
    assert r2.error_code == "redirect_loop" and len(r2.redirects) == 4


async def test_redirect_followed_relative_and_final_url():
    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new?utm_source=x"})
        return httpx.Response(200, text="<html>hi</html>", headers={"Content-Type": "text/html"})

    f = mock_fetcher(handler)
    r = await f.fetch("http://public.example/old")
    await f.aclose()
    assert r.ok and r.final_url == "http://public.example/new?utm_source=x" and r.redirects == ["http://public.example/old"]


async def test_idn_to_punycode_and_port_allow_list():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text="x", headers={"Content-Type": "text/plain"})

    f = mock_fetcher(handler)
    r = await f.fetch("http://bücher.example/ü")
    assert r.ok and seen[0].startswith("http://xn--bcher-kva.example/")
    r2 = await f.fetch("http://example.com:9999/")
    assert r2.error_code == "blocked_ssrf"
    await f.aclose()
    f2 = mock_fetcher(handler, allowed_ports={80, 443, 9999})
    r3 = await f2.fetch("http://example.com:9999/")
    await f2.aclose()
    assert r3.ok


async def test_user_agent_includes_contact_email():
    s = get_settings().model_copy(update={"contact_email": "ops@example.org"})
    got = {}

    def handler(req):
        got["ua"] = req.headers["user-agent"]
        return httpx.Response(200, text="x", headers={"Content-Type": "text/plain"})

    f = SafeFetcher(s, resolver=public_resolver, transport=httpx.MockTransport(handler), allow_private_networks=False)
    await f.fetch("http://public.example/")
    await f.aclose()
    assert "LeadIntelBot" in got["ua"] and "ops@example.org" in got["ua"]


# ---------------------------------------------------------------- real local server behaviour
async def test_size_cap_truncates_html(server, fetcher):
    server.add("/big", b"<html>" + b"a" * 3_000_000 + b"</html>")
    r = await fetcher.fetch(server.url("/big"), max_bytes=100_000)
    assert r.error_code == "too_large" and r.truncated and len(r.body) == 100_000 and r.usable_body


async def test_size_cap_pdf_declared_too_large_aborts_without_body(server, fetcher):
    server.add("/big.pdf", b"%PDF-1.4" + b"0" * 2_000_000, ctype="application/pdf")
    r = await fetcher.fetch(server.url("/big.pdf"), max_bytes=100_000)
    assert r.error_code == "too_large" and r.body == b"" and not r.usable_body


async def test_endless_chunked_stream_aborted_early(server, fetcher):
    server.add("/endless", b"", chunked_forever=True)
    t = time.monotonic()
    r = await fetcher.fetch(server.url("/endless"), max_bytes=50_000)
    assert r.error_code == "too_large" and len(r.body) == 50_000
    assert time.monotonic() - t < 5


async def test_gzip_handled_and_gzip_bomb_capped(server, fetcher):
    server.add("/gz", "<html>héllo gzip</html>", gzip_=True)
    r = await fetcher.fetch(server.url("/gz"))
    assert r.ok and "héllo gzip" in r.text()
    bomb = gzip.compress(b"0" * 30_000_000)
    server.add("/bomb", lambda h: bomb, ctype="text/plain", headers={"Content-Encoding": "gzip"})
    r2 = await fetcher.fetch(server.url("/bomb"), max_bytes=200_000)
    assert r2.error_code == "too_large" and len(r2.body) <= 200_000


async def test_content_type_allow_list(server, fetcher):
    server.add("/img", b"\x89PNG....", ctype="image/png")
    server.add("/zip", b"PK..", ctype="application/zip")
    server.add("/pdf", b"%PDF-1.4 hi", ctype="application/pdf")
    server.add("/vcf", b"BEGIN:VCARD\nEND:VCARD", ctype="text/vcard")
    server.add("/json", b"{}", ctype="application/json")
    server.add("/nothing", b"<html>x</html>", ctype="")
    for path in ("/img", "/zip"):
        r = await fetcher.fetch(server.url(path))
        assert r.error_code == "bad_content_type" and r.body == b"", path
    for path in ("/pdf", "/vcf", "/json"):
        assert (await fetcher.fetch(server.url(path))).ok, path


async def test_http_errors_are_statuses_not_exceptions(server, fetcher):
    server.add("/gone", "<html>gone</html>", status=410)
    server.add("/boom", "oops", status=500)
    r = await fetcher.fetch(server.url("/gone"))
    r2 = await fetcher.fetch(server.url("/boom"))
    assert (r.status, r.ok, r.error_code) == (410, False, None)
    assert r2.status == 500 and not r2.ok


async def test_timeout_typed(server, allow_private):
    s = get_settings().model_copy(update={"http_timeout_s": 0.4})
    server.add("/slow", "<html>slow</html>", delay=2.0)
    f = SafeFetcher(s)
    r = await f.fetch(server.url("/slow"))
    await f.aclose()
    assert r.error_code == "timeout"


async def test_connection_refused_typed(allow_private):
    f = SafeFetcher()
    r = await f.fetch("http://127.0.0.1:1/")
    await f.aclose()
    assert r.error_code == "connection_error"


def test_tls_failure_classification():
    exc = httpx.ConnectError("boom")
    exc.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    assert sh.classify_exception(exc)[0] == "tls_failure"
    assert sh.classify_exception(httpx.ReadTimeout("t"))[0] == "timeout"


# ---------------------------------------------------------------- conditional requests
async def test_conditional_request_304_path(server, fetcher):
    server.add("/page", "<html>stable</html>", etag='"v1"')
    cache = MemoryCacheStore()
    r1 = await fetcher.fetch(server.url("/page"), cache=cache)
    assert r1.ok and not r1.not_modified and r1.body
    r2 = await fetcher.fetch(server.url("/page"), cache=cache)
    assert r2.status == 304 and r2.not_modified and r2.from_cache and r2.ok and r2.body == b""
    assert r2.content_hash == r1.content_hash
    assert server.requests[-1][1].get("if-none-match") == '"v1"'
    # content changes -> full body again
    server.add("/page", "<html>changed</html>", etag='"v2"')
    r3 = await fetcher.fetch(server.url("/page"), cache=cache)
    assert r3.status == 200 and b"changed" in r3.body


async def test_db_cache_store_roundtrip(server, fetcher, session):
    server.add("/p", "<html>x</html>", etag='"abc"')
    store = DbCacheStore()
    r1 = await fetcher.fetch(server.url("/p"), cache=store)
    assert r1.ok
    entry = await store.get(server.url("/p"))
    assert entry and entry.etag == '"abc"' and entry.status == 200 and entry.content_hash == r1.content_hash
    r2 = await fetcher.fetch(server.url("/p"), cache=store)
    assert r2.not_modified


# ---------------------------------------------------------------- decoding
def test_decode_body_never_crashes():
    assert decode_body("héllo".encode("cp1252"), "text/html") == "héllo"  # not valid utf-8 -> cp1252
    assert decode_body("héllo".encode(), "text/html") == "héllo"
    assert decode_body("héllo".encode("latin-1"), "text/html; charset=iso-8859-1") == "héllo"
    assert decode_body(b'<meta charset="windows-1252"><p>caf\xe9</p>', None).endswith("café</p>")
    assert decode_body(b"abc\xff\xfe", "text/html; charset=bogus-charset") is not None
    assert decode_body(b"\xef\xbb\xbfhi", None) == "hi"
    assert decode_body("日本語".encode("utf-16"), None) == "日本語"
    assert decode_body(b"", None) == ""
    assert decode_body(bytes(range(256)), "text/html; charset=utf-8")  # lenient fallback


async def test_encoding_errors_in_real_response(server, fetcher):
    server.add("/latin", b"<html><body>caf\xe9 \x81\x8d info@x.com</body></html>", ctype="text/html")
    r = await fetcher.fetch(server.url("/latin"))
    assert r.ok and "caf" in r.text()


def test_dns_failure_class_exported():
    assert issubclass(DnsFailure, Exception) and sh.ERROR_CODES >= {"dns_failure", "blocked_ssrf", "too_large"}


# ---------------------------------------------------------------- TLS: connect by pinned IP, verify against the host name
def _selfsigned(tmp_path, host):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256()))
    cp, kp = tmp_path / "c.pem", tmp_path / "k.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    return str(cp), str(kp)


async def test_tls_sni_and_cert_verified_against_hostname_while_pinned_to_ip(tmp_path, allow_private):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cert, key = _selfsigned(tmp_path, "secure.test")
    seen_sni = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>secure ok</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert, key)
    sctx.sni_callback = lambda sock, name, ctx: seen_sni.append(name)
    httpd.socket = sctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    async def resolver(host, p):
        return ["127.0.0.1"]

    good = ssl.create_default_context(cafile=cert)
    f = SafeFetcher(resolver=resolver, allowed_ports={port}, verify=good)
    r = await f.fetch(f"https://secure.test:{port}/")
    assert r.ok and b"secure ok" in r.body and seen_sni == ["secure.test"]
    await f.aclose()
    # a certificate for another name must be rejected (hostname checked, not the IP)
    f2 = SafeFetcher(resolver=resolver, allowed_ports={port}, verify=good)
    r2 = await f2.fetch(f"https://other-name.test:{port}/")
    await f2.aclose()
    httpd.shutdown()
    assert r2.error_code == "tls_failure"
