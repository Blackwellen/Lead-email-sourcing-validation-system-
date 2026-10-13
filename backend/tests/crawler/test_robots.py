import httpx

from app.crawler.robots import Decision, RobotsCache, parse_robots
from app.crawler.safe_http import SafeFetcher

UA = "leadintelbot"


def test_parse_precedence_wildcards_and_sitemaps():
    r = parse_robots("""
# comment
User-agent: *
Disallow: /private
Allow: /private/public
Disallow: /*.pdf$
Disallow: /tmp*/cache
Crawl-delay: 7
Sitemap: https://x.test/sitemap.xml

User-agent: LeadIntelBot
Disallow: /bot-only
""")
    assert r.sitemaps == ["https://x.test/sitemap.xml"]
    # specific group wins over '*': only /bot-only is blocked for us
    assert not r.is_allowed(UA, "/bot-only/x")
    assert r.is_allowed(UA, "/private/x")
    assert r.crawl_delay(UA) is None
    # for other agents the '*' group applies
    assert not r.is_allowed("otherbot", "/private/a")
    assert r.is_allowed("otherbot", "/private/public/a")  # longest match (Allow) wins
    assert not r.is_allowed("otherbot", "/files/x.pdf")
    assert r.is_allowed("otherbot", "/files/x.pdf?download=1")  # `$` anchors the end
    assert not r.is_allowed("otherbot", "/tmp123/cache")
    assert r.crawl_delay("otherbot") == 7.0


def test_allow_wins_tie_and_empty_disallow():
    r = parse_robots("User-agent: *\nDisallow: /a\nAllow: /a\n")
    assert r.is_allowed("x", "/a")
    r2 = parse_robots("User-agent: *\nDisallow:\n")
    assert r2.is_allowed("x", "/anything")
    r3 = parse_robots("User-agent: *\nDisallow: /\n")
    assert not r3.is_allowed("x", "/")


def test_consecutive_user_agents_share_group_and_percent_encoding():
    r = parse_robots("User-agent: a\nUser-agent: b\nDisallow: /x%2Fy\nDisallow: /caf%c3%a9\n")
    assert not r.is_allowed("a", "/x%2fy")
    assert not r.is_allowed("b", "/caf%C3%A9")
    assert r.is_allowed("c", "/x")


def test_garbage_robots_is_allow_all():
    r = parse_robots("<html><body>Not found</body></html>\n\x00\x01 junk: here")
    assert r.is_allowed(UA, "/anything")


def make(handler):
    f = SafeFetcher(resolver=_pub, transport=httpx.MockTransport(handler), allow_private_networks=False)
    return f, RobotsCache(f)


async def _pub(host, port):
    return ["93.184.216.34"]


async def test_cache_allow_disallow_and_delay():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, text="User-agent: *\nDisallow: /admin\nCrawl-delay: 3\nSitemap: https://a.test/s.xml",
                              headers={"Content-Type": "text/plain"})

    f, rc = make(handler)
    c1 = await rc.check("https://a.test/contact")
    c2 = await rc.check("https://a.test/admin/x")
    assert c1.decision is Decision.ALLOWED and c1.crawl_delay == 3.0
    assert c2.decision is Decision.DISALLOWED
    assert calls["n"] == 1  # cached per origin
    assert await rc.sitemaps("https://a.test/") == ["https://a.test/s.xml"]
    await f.aclose()


async def test_4xx_means_allow_all():
    for status in (404, 403, 401, 410):
        f, rc = make(lambda req, s=status: httpx.Response(s, text="nope"))
        assert (await rc.check("https://a.test/x")).decision is Decision.ALLOWED, status
        await f.aclose()


async def test_5xx_and_unreachable_defer():
    for status in (500, 503, 429):
        f, rc = make(lambda req, s=status: httpx.Response(s, text="err"))
        c = await rc.check("https://a.test/x")
        assert c.decision is Decision.DEFERRED, status
        assert not c.allowed
        await f.aclose()

    def boom(req):
        raise httpx.ConnectError("down")

    f, rc = make(boom)
    assert (await rc.check("https://a.test/")).decision is Decision.DEFERRED
    await f.aclose()


async def test_deferred_entry_expires_and_recovers():
    t = {"now": 0.0}
    state = {"status": 503}

    def handler(req):
        if state["status"] == 503:
            return httpx.Response(503)
        return httpx.Response(200, text="User-agent: *\nDisallow:\n", headers={"Content-Type": "text/plain"})

    f = SafeFetcher(resolver=_pub, transport=httpx.MockTransport(handler), allow_private_networks=False)
    rc = RobotsCache(f, deferred_ttl=60, clock=lambda: t["now"])
    assert (await rc.check("https://a.test/")).decision is Decision.DEFERRED
    state["status"] = 200
    t["now"] = 30
    assert (await rc.check("https://a.test/")).decision is Decision.DEFERRED  # still within deferral window
    t["now"] = 61
    assert (await rc.check("https://a.test/")).decision is Decision.ALLOWED
    await f.aclose()


async def test_ssrf_blocked_robots_origin_is_not_allowed():
    f = SafeFetcher(resolver=_pub, transport=httpx.MockTransport(lambda r: httpx.Response(200)), allow_private_networks=False)
    rc = RobotsCache(f)
    assert not (await rc.check("http://169.254.169.254/x")).allowed
    await f.aclose()


async def test_concurrent_checks_fetch_once():
    import asyncio

    n = {"c": 0}

    async def slow(req):
        n["c"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, text="User-agent: *\nDisallow: /x", headers={"Content-Type": "text/plain"})

    f = SafeFetcher(resolver=_pub, transport=httpx.MockTransport(slow), allow_private_networks=False)
    rc = RobotsCache(f)
    res = await asyncio.gather(*[rc.check(f"https://a.test/p{i}") for i in range(10)])
    assert n["c"] == 1 and all(r.allowed for r in res)
    await f.aclose()
