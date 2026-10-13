import asyncio
import gzip

import httpx
import pytest

from app.crawler.crawl import (
    CrawlOptions,
    crawl_domain,
    detect_parked,
    playwright_available,
    render_with_playwright,
)
from app.crawler.extract import extract_from_html
from app.crawler.ratelimit import RateLimiter
from app.crawler.robots import RobotsCache
from app.crawler.safe_http import DnsFailure, MemoryCacheStore, SafeFetcher
from tests.crawler.pdfgen import make_pdf


def opts(**kw):
    kw.setdefault("time_budget_s", 20)
    return CrawlOptions(**kw)


def lim():
    return RateLimiter(min_interval=0)


NAV = """<nav><a href="/">Home</a><a href="/contact">Contact</a><a href="/about">About</a><a href="/team">Team</a>
<a href="/locations">Locations</a><a href="/services/boilers">Boilers</a><a href="/blog/post-1">Blog</a>
<a href="/brochure.pdf">Brochure</a><a href="/login">Login</a><a href="/private/area">Private</a>
<a href="{other}/contact">Other host</a><a href="https://elsewhere.example.org/contact">Elsewhere</a></nav>"""


def build_site(server, *, robots=None):
    other = f"http://localhost:{server.port}"
    server.add("/", f"<html><head><title>Acme Plumbing Ltd</title></head><body>{NAV.format(other=other)}"
                    "<h1>Acme Plumbing</h1><footer>Acme Plumbing Ltd. Registered in England and Wales No. 01234567. SW1A 1AA</footer></body></html>")
    server.add("/contact", "<html><body><h1>Contact</h1><a href='mailto:info@acme.co.uk'>info@acme.co.uk</a>"
                           "<p>Sales: sales@acme.co.uk</p><a href='/contact/deep'>deeper</a></body></html>")
    server.add("/contact/deep", "<html><body><a href='/contact/deep/deeper'>x</a><p>deep@acme.co.uk</p></body></html>")
    server.add("/contact/deep/deeper", "<html><body>too-deep@acme.co.uk</body></html>")
    server.add("/about", "<html><body><p>About us. info@acme.co.uk</p></body></html>")
    server.add("/team", "<html><body><div class='team-member'><h3>John Smith</h3><p>Director</p>"
                        "<a href='mailto:john@acme.co.uk'>mail</a></div></body></html>")
    server.add("/locations", "<html><body><address>1 High St</address><a href='mailto:leeds@acme.co.uk'>l</a></body></html>")
    server.add("/services/boilers", "<html><body>boiler@acme.co.uk</body></html>")
    server.add("/blog/post-1", "<html><body>blog@acme.co.uk</body></html>")
    server.add("/private/area", "<html><body>private@acme.co.uk</body></html>")
    server.add("/brochure.pdf", make_pdf(["Brochure", "pdf-sales@acme.co.uk"]), ctype="application/pdf")
    server.add("/robots.txt", robots or "User-agent: *\nDisallow: /private\nSitemap: " + server.url("/sitemap.xml"), ctype="text/plain")
    server.add("/sitemap.xml", f"""<urlset><url><loc>{server.url('/offices')}</loc></url>
        <url><loc>{server.url('/private/secret')}</loc></url><url><loc>https://elsewhere.example.org/contact</loc></url></urlset>""",
               ctype="application/xml")
    server.add("/offices", "<html><body>offices@acme.co.uk</body></html>")


async def crawl(fetcher, server, **kw):
    return await crawl_domain(fetcher, server.url("/"), options=kw.pop("options", opts()), limiter=lim(), **kw)


def fetched_paths(server):
    return [p.split("?")[0] for p, _ in server.requests]


async def test_full_crawl_priorities_dedupe_and_provenance(server, fetcher):
    build_site(server)
    r = await crawl(fetcher, server, options=opts(keywords=("boiler",)))
    got = {e.email for e in r.emails}
    assert {"info@acme.co.uk", "sales@acme.co.uk", "john@acme.co.uk", "leeds@acme.co.uk", "boiler@acme.co.uk",
            "offices@acme.co.uk", "pdf-sales@acme.co.uk", "deep@acme.co.uk"} <= got
    assert "blog@acme.co.uk" not in got and "private@acme.co.uk" not in got  # blog not prioritised; robots
    assert "too-deep@acme.co.uk" not in got  # depth 3
    info = next(e for e in r.emails if e.email == "info@acme.co.uk")
    assert len(info.page_urls) >= 2 and info.methods >= {"mailto"}  # contact + about
    assert next(e for e in r.emails if e.email == "john@acme.co.uk").name == "John Smith"
    assert r.stats.unique_emails == len(r.emails) and r.stats.observations == len(r.observations)
    assert r.health.reachable and r.health.parked is None and r.health.status == 200
    assert r.stats.pdfs == 1 and r.stats.sitemap_urls == 3 and r.stats.pages_ok >= 7
    # contact-ish pages come before generic ones
    order = [p.url for p in r.pages if p.kind == "page"]
    assert order[0].endswith("/") and "/contact" in order[1]
    assert any("/team" in o for o in order[:4])
    ev = r.site_evidence("registry")
    assert "01234567" in ev.company_numbers and "SW1A 1AA" in ev.postcodes and ev.source_kind == "registry"
    assert ev.titles[0] == "Acme Plumbing Ltd" and ev.reachable
    assert all(not p.url.endswith("/login") for p in r.pages)


async def test_frontier_limits(server, fetcher):
    build_site(server)
    r = await crawl(fetcher, server, options=opts(max_pages=3, max_pdfs=0, use_sitemap=False))
    pages = [p for p in r.pages if p.kind == "page"]
    assert len(pages) <= 3 and r.stopped_reason == "max_pages"
    server.requests.clear()
    r2 = await crawl(fetcher, server, options=opts(max_depth=1, max_pdfs=0))
    assert "/contact/deep" not in fetched_paths(server)  # depth-2 link not followed with max_depth=1
    assert "/offices" in fetched_paths(server)  # sitemap-discovered
    server.requests.clear()
    await crawl(fetcher, server, options=opts(max_pdfs=0))
    assert "/brochure.pdf" not in fetched_paths(server)
    assert r2.stats.pdfs == 0


async def test_same_registrable_domain_only(server, fetcher):
    build_site(server)
    await crawl(fetcher, server)
    hosts = {h.get("host", "").split(":")[0] for _, h in server.requests}
    assert hosts == {"127.0.0.1"}  # never requested localhost or elsewhere.example.org


async def test_robots_disallow_respected_and_logged(server, fetcher):
    build_site(server)
    r = await crawl(fetcher, server)
    assert "/private/area" not in fetched_paths(server) and "/private/secret" not in fetched_paths(server)
    assert any(p.error_code == "robots_disallowed" for p in r.pages) or r.stats.robots_blocked == 0
    server.requests.clear()
    build_site(server, robots="User-agent: *\nDisallow: /\n")
    r2 = await crawl(fetcher, server)
    assert r2.health.robots_blocked_homepage and r2.emails == []
    assert fetched_paths(server) == ["/robots.txt"]


async def test_robots_5xx_defers_everything(server, fetcher):
    build_site(server)
    server.add("/robots.txt", "boom", status=503, ctype="text/plain")
    r = await crawl(fetcher, server)
    assert r.health.robots_deferred and r.emails == [] and not r.health.reachable
    assert set(fetched_paths(server)) == {"/robots.txt"}


async def test_robots_crawl_delay_honoured(server, fetcher):
    build_site(server, robots="User-agent: *\nCrawl-delay: 0.2\n")
    limiter = RateLimiter(min_interval=0)
    import time
    t = time.monotonic()
    r = await crawl_domain(fetcher, server.url("/"), options=opts(max_pages=4, max_pdfs=0, use_sitemap=False), limiter=limiter)
    pages = len([p for p in r.pages if p.kind == "page"])
    assert pages >= 3 and time.monotonic() - t >= 0.2 * (pages - 1) * 0.9


async def test_429_with_retry_after_stops_politely(server, fetcher):
    build_site(server)
    server.add("/", "<html>busy</html>", status=429, headers={"Retry-After": "600"})
    r = await crawl(fetcher, server, options=opts(max_rate_wait_s=0.5))
    assert r.stopped_reason in ("rate_limited",) or r.stopped_reason.startswith("homepage_unreachable")
    assert r.health.rate_limited and server.hits("/") <= 2 and r.emails == []


async def test_429_recovers_after_short_retry_after(server, fetcher):
    build_site(server)
    server.routes["/"].statuses = [429]
    server.routes["/"].headers = {"Retry-After": "0"}
    r = await crawl(fetcher, server)
    assert r.health.reachable and server.hits("/") == 2 and r.emails


async def test_inner_page_429_stops_domain_but_keeps_results(server, fetcher):
    build_site(server)
    server.add("/contact", "slow down", status=429, headers={"Retry-After": "900"})
    r = await crawl(fetcher, server, options=opts(max_rate_wait_s=0.3))
    assert r.stopped_reason == "rate_limited" and r.health.reachable
    assert server.hits("/contact") <= 2


async def test_large_page_truncated_but_extracted(server, fetcher):
    body = b"<html><body><a href='mailto:early@acme.co.uk'>x</a>" + b"<p>filler</p>" * 400_000 + b"late@acme.co.uk</body></html>"
    server.add("/", body)
    server.add("/robots.txt", "", ctype="text/plain")
    r = await crawl(fetcher, server, options=opts(max_page_bytes=100_000, use_sitemap=False))
    home = r.pages[0]
    assert home.truncated and home.error_code == "too_large" and home.bytes == 100_000
    assert {e.email for e in r.emails} == {"early@acme.co.uk"}
    assert r.health.reachable


async def test_conditional_requests_on_recrawl(server, allow_private):
    build_site(server)
    for path in ("/", "/contact", "/about", "/team"):
        server.routes[path].etag = f'"{path}"'
    f = SafeFetcher(cache=MemoryCacheStore())
    r1 = await crawl_domain(f, server.url("/"), options=opts(max_pdfs=0, use_sitemap=False), limiter=lim())
    r2 = await crawl_domain(f, server.url("/"), options=opts(max_pdfs=0, use_sitemap=False), limiter=lim())
    await f.aclose()
    assert r1.stats.not_modified == 0
    assert r2.stats.not_modified >= 3 and any(p.not_modified for p in r2.pages)
    assert not next(p for p in r2.pages if p.reason == 'homepage').not_modified


async def test_dns_failure_recorded_no_raise(allow_private):
    async def nx(host, port):
        raise DnsFailure("nxdomain")

    f = SafeFetcher(resolver=nx)
    r = await crawl_domain(f, "no-such-domain.example", options=opts(), limiter=lim())
    await f.aclose()
    assert r.health.dns_failure and not r.health.reachable and r.stats.errors.get("dns_failure", 0) >= 1
    assert r.emails == [] and r.stopped_reason.startswith("homepage_unreachable")


async def test_tls_failure_recorded_and_http_fallback():
    import ssl

    async def pub(h, p):
        return ["93.184.216.34"]

    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.scheme == "https":
            e = httpx.ConnectError("tls")
            e.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
            raise e
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>hi tls@acme.co.uk</body></html>", headers={"Content-Type": "text/html"})

    f = SafeFetcher(resolver=pub, transport=httpx.MockTransport(handler), allow_private_networks=False)
    r = await crawl_domain(f, "tls-broken.example", options=opts(use_sitemap=False), limiter=lim())
    await f.aclose()
    assert r.health.tls_failure and r.health.reachable and {e.email for e in r.emails} == {"tls@acme.co.uk"}
    assert any(u.startswith("http://") for u in seen)


async def test_ssrf_target_domain_blocked_not_fetched():
    f = SafeFetcher(allow_private_networks=False)
    for target in ("169.254.169.254", "http://127.0.0.1:8000/", "localhost", "10.0.0.1"):
        r = await crawl_domain(f, target, options=opts(), limiter=lim())
        assert r.health.blocked_ssrf and not r.health.reachable, target
    await f.aclose()


async def test_bad_input_never_raises(fetcher):
    for bad in ["", "   ", "http://", "://x", "a b c", "file:///etc/passwd"]:
        r = await crawl_domain(fetcher, bad, options=opts(), limiter=lim())
        assert not r.health.reachable and r.emails == []


@pytest.mark.parametrize("html,reason", [
    ("<html><head><title>example.com</title></head><body><h1>This domain is for sale</h1><p>Buy this domain today</p></body></html>", "domain_for_sale"),
    ("<html><body><p>Welcome to nginx!</p><p>If you see this page, the nginx web server is successfully installed</p></body></html>", "default_page"),
    ("<html><body><h1>Domain Expired</h1><p>This domain has expired. Renew your domain now.</p></body></html>", "expired"),
    ("<html><body><h1>Account Suspended</h1><p>This account has been suspended.</p></body></html>", "suspended"),
    ("<html><body><p>The domain is parked free, courtesy of GoDaddy.com</p></body></html>", "parked"),
])
async def test_parked_detection_stops_crawl(server, fetcher, html, reason):
    server.add("/", html)
    server.add("/contact", "<html><body>should-not@acme.co.uk</body></html>")
    r = await crawl(fetcher, server)
    assert r.health.parked == reason and r.stopped_reason == f"parked:{reason}"
    assert "/contact" not in fetched_paths(server)
    assert r.site_evidence().parked == reason


def test_detect_parked_has_no_false_positive_on_real_business_page():
    x = extract_from_html("<html><head><title>Acme Plumbing</title></head><body><h1>Boiler repair</h1>"
                          + "<p>We service boilers across Leeds. Domain knowledge matters. Our parked-car cleaning service.</p>" * 30
                          + "</body></html>", "https://acme.co.uk/")
    assert detect_parked(x, "https://acme.co.uk/") is None
    assert detect_parked(None, "https://sedoparking.com/x") == "parking_host"


async def test_redirect_to_different_domain_noted_not_followed(server, fetcher):
    server.add("/", b"", status=301, headers={"Location": f"http://localhost:{server.port}/home"})
    server.add("/home", "<html><head><title>New Brand</title></head><body>hi new@acme.co.uk<a href='/contact'>Contact</a></body></html>")
    server.add("/contact", "<html>c@acme.co.uk</html>")
    server.add("/robots.txt", "", ctype="text/plain")
    r = await crawl(fetcher, server)
    assert r.health.redirected_domain == "localhost" and r.health.reachable
    assert r.stopped_reason == "redirected_to_other_domain"
    assert "/contact" not in fetched_paths(server)
    assert r.site_evidence().redirected_domain == "localhost"
    r2 = await crawl(fetcher, server, options=opts(follow_cross_domain_redirect=True, use_sitemap=False))
    assert "c@acme.co.uk" in {e.email for e in r2.emails}


async def test_www_equivalence_on_same_site(server, fetcher):
    # links to the same host spelled with a trailing dot / different case are the same site
    server.add("/", f"<html><a href='HTTP://127.0.0.1:{server.port}/contact'>Contact</a><a href='http://127.0.0.1:{server.port}/contact#frag'>c2</a></html>")
    server.add("/contact", "<html>c@acme.co.uk</html>")
    server.add("/robots.txt", "", ctype="text/plain")
    await crawl(fetcher, server, options=opts(use_sitemap=False, guess_standard_paths=False))
    assert server.hits("/contact") == 1  # de-duplicated before fetching


async def test_gzip_sitemap_discovers_unlinked_contact_page(server, fetcher):
    server.add("/", "<html><body>no links here</body></html>")
    server.add("/robots.txt", f"Sitemap: {server.url('/sm.xml.gz')}", ctype="text/plain")
    server.add("/sm.xml.gz", gzip.compress(f"<urlset><url><loc>{server.url('/kontakt')}</loc></url></urlset>".encode()),
               ctype="application/gzip")
    server.add("/kontakt", "<html>kontakt@acme.de</html>")
    r = await crawl(fetcher, server)
    assert "kontakt@acme.de" in {e.email for e in r.emails}


async def test_standard_path_probes_when_no_links(server, fetcher):
    server.add("/", "<html><body>js only</body></html>")
    server.add("/robots.txt", "", ctype="text/plain")
    server.add("/contact", "<html>probe@acme.co.uk</html>")
    r = await crawl(fetcher, server, options=opts(use_sitemap=False))
    assert "probe@acme.co.uk" in {e.email for e in r.emails}


async def test_on_page_callbacks_sync_and_async(server, fetcher):
    build_site(server)
    sync_seen, async_seen = [], []

    def cb(rec, x):
        sync_seen.append(rec.url)

    async def acb(rec, x):
        await asyncio.sleep(0)
        async_seen.append((rec.url, None if x is None else len(x.emails)))

    r1 = await crawl(fetcher, server, on_page=cb, options=opts(max_pdfs=0, use_sitemap=False))
    await crawl(fetcher, server, on_page=acb, options=opts(max_pdfs=0, use_sitemap=False))
    assert len(sync_seen) == len([p for p in r1.pages if p.kind != "sitemap"]) and async_seen

    def boom(rec, x):
        raise RuntimeError("callback bug")

    r3 = await crawl(fetcher, server, on_page=boom, options=opts(max_pdfs=0, use_sitemap=False))
    assert r3.emails  # callback failures never break the crawl


async def test_time_budget(server, fetcher):
    build_site(server)
    server.routes["/contact"].delay = 0.8
    server.routes["/about"].delay = 0.8
    r = await crawl(fetcher, server, options=opts(time_budget_s=1.0, use_sitemap=False, max_pdfs=0))
    assert r.stopped_reason == "time_budget" or len(r.pages) < 8
    assert r.stats.elapsed_s < 4


async def test_non_html_and_error_pages_recorded(server, fetcher):
    server.add("/", "<html><a href='/contact'>c</a><a href='/team'>t</a><a href='/about'>a</a></html>")
    server.add("/robots.txt", "", ctype="text/plain")
    server.add("/contact", "gone", status=404)
    server.add("/team", b"\x89PNG", ctype="image/png")
    server.add("/about", "boom", status=500)
    r = await crawl(fetcher, server, options=opts(use_sitemap=False))
    codes = {p.url.rsplit("/", 1)[-1]: p.error_code for p in r.pages}
    assert codes["contact"] == "http_404" and codes["team"] == "bad_content_type" and codes["about"] == "http_500"
    assert r.stats.pages_failed >= 3 and r.stats.errors["bad_content_type"] == 1


async def test_js_render_hook_used_only_when_enabled_and_robots_allow(server, fetcher):
    shell = "<html><head><script src='/app.js'></script></head><body><div id=root></div></body></html>"
    server.add("/", shell)
    server.add("/robots.txt", "", ctype="text/plain")
    calls = []

    async def fake(url):
        calls.append(url)
        return "<html><body><p>Rendered: js@acme.co.uk</p></body></html>"

    r0 = await crawl(fetcher, server, options=opts(use_sitemap=False, js_renderer=fake))
    assert calls == [] and r0.emails == []
    r1 = await crawl(fetcher, server, options=opts(use_sitemap=False, js_renderer=fake, allow_js_render=True))
    assert len(calls) == 1 and {e.email for e in r1.emails} == {"js@acme.co.uk"} and r1.stats.js_rendered == 1
    assert r1.pages[0].from_js
    # robots disallow -> homepage never fetched, renderer never called
    calls.clear()
    server.add("/robots.txt", "User-agent: *\nDisallow: /\n", ctype="text/plain")
    await crawl(fetcher, server, options=opts(use_sitemap=False, js_renderer=fake, allow_js_render=True))
    assert calls == []


async def test_js_not_used_when_static_emails_exist(server, fetcher):
    server.add("/", "<html><head><script src='/a.js'></script></head><body>static@acme.co.uk</body></html>")
    server.add("/robots.txt", "", ctype="text/plain")
    calls = []

    async def fake(url):
        calls.append(url)
        return None

    await crawl(fetcher, server, options=opts(use_sitemap=False, js_renderer=fake, allow_js_render=True))
    assert calls == []


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
async def test_real_playwright_render(server, allow_private):
    from app.crawler.safe_http import Guard

    server.add("/", "<html><body><div id=r></div><script>document.getElementById('r').textContent='pw@acme.co.uk'</script></body></html>")
    try:
        html = await render_with_playwright(server.url("/"), guard=Guard(allow_private=True))
    except Exception as e:  # browsers not installed in this environment
        pytest.skip(f"chromium unavailable: {e}")
    if html is None:
        pytest.skip("chromium unavailable")
    assert "pw@acme.co.uk" in html


async def test_playwright_absent_returns_none_for_blocked_url():
    from app.crawler.safe_http import Guard

    assert await render_with_playwright("http://169.254.169.254/", guard=Guard(allow_private=False)) is None


async def test_robots_object_shared_across_crawls(server, fetcher):
    build_site(server)
    rc = RobotsCache(fetcher)
    await crawl_domain(fetcher, server.url("/"), options=opts(max_pdfs=0), limiter=lim(), robots=rc)
    n = server.hits("/robots.txt")
    await crawl_domain(fetcher, server.url("/"), options=opts(max_pdfs=0), limiter=lim(), robots=rc)
    assert n == 1 and server.hits("/robots.txt") == 1


class _FakeReq:
    def __init__(self, url, resource_type="document", method="GET"):
        self.url, self.resource_type, self.method = url, resource_type, method


class _FakeRoute:
    def __init__(self, req):
        self.request = req
        self.outcome = None

    async def abort(self, code="failed"):
        self.outcome = ("abort", code)

    async def continue_(self):
        self.outcome = ("continue", None)

    async def fulfill(self, status=200, headers=None, body=b""):
        self.outcome = ("fulfill", status, body)


async def test_render_gate_isolates_the_browser(server, allow_private, monkeypatch):
    """Finding 10: the browser never connects itself; every request is guard-checked and fetched by SafeFetcher."""
    from app.config import get_settings
    from app.crawler.crawl import BROWSER_ARGS, render_gate
    from app.crawler.safe_http import SafeFetcher

    assert "--host-resolver-rules=MAP * ~NOTFOUND" in BROWSER_ARGS
    server.add("/", "<html><body>hello render</body></html>")
    f = SafeFetcher()
    try:
        ok = _FakeRoute(_FakeReq(server.url("/")))
        await render_gate(ok, f)
        assert ok.outcome[0] == "fulfill" and b"hello render" in ok.outcome[2]
        for req in (_FakeReq("file:///etc/passwd"), _FakeReq("ws://example.com/socket", "websocket"), _FakeReq("ftp://example.com/x"),
                    _FakeReq(server.url("/"), "image"), _FakeReq(server.url("/"), "fetch", "POST")):
            r = _FakeRoute(req)
            await render_gate(r, f)
            assert r.outcome[0] == "abort", req.url
        local = _FakeRoute(_FakeReq("data:text/html,hi"))
        await render_gate(local, f)
        assert local.outcome[0] == "continue"
        monkeypatch.setattr(get_settings(), "allow_private_networks", False)
        for url in ("http://169.254.169.254/latest/meta-data/", server.url("/"), "http://localhost/"):
            r = _FakeRoute(_FakeReq(url, "xhr"))
            await render_gate(r, f)
            assert r.outcome[0] == "abort", url
    finally:
        await f.aclose()
