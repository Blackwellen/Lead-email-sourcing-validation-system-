import gzip

from app.crawler.sitemap import parse_sitemap, pick_contact_urls

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <url><loc>https://acme.co.uk/</loc></url>
 <url><loc>https://acme.co.uk/contact-us</loc><lastmod>2026-01-01</lastmod></url>
 <url><loc>https://acme.co.uk/contact-us?utm_source=x</loc></url>
 <url><loc> https://acme.co.uk/our-team </loc></url>
 <url><loc>https://acme.co.uk/blog/2024/01/post-1</loc></url>
 <url><loc>https://acme.co.uk/brochure.pdf</loc></url>
 <url><loc>https://other.com/contact</loc></url>
 <url><loc>javascript:alert(1)</loc></url>
</urlset>"""

INDEX = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://acme.co.uk/sitemap-pages.xml</loc></sitemap><sitemap><loc>https://acme.co.uk/sitemap-posts.xml.gz</loc></sitemap>
<sitemap><loc>https://acme.co.uk/sitemap-pages.xml</loc></sitemap></sitemapindex>"""


def test_urlset_dedup_normalise_and_pick():
    sm = parse_sitemap(URLSET)
    assert sm.error is None and not sm.is_index
    assert sm.urls.count("https://acme.co.uk/contact-us") == 1  # utm variant collapses
    assert "javascript:alert(1)" not in sm.urls and "https://acme.co.uk/our-team" in sm.urls
    picks = pick_contact_urls(sm.urls, "https://acme.co.uk/", limit=5)
    got = [p.url for p in picks]
    assert got[0] == "https://acme.co.uk/contact-us" and "https://acme.co.uk/our-team" in got
    assert "https://other.com/contact" not in got and not any("blog" in u for u in got)


def test_index_and_gzip_and_duplicates():
    sm = parse_sitemap(INDEX)
    assert sm.is_index and sm.sitemaps == ["https://acme.co.uk/sitemap-pages.xml", "https://acme.co.uk/sitemap-posts.xml.gz"]
    gz = gzip.compress(URLSET)
    assert parse_sitemap(gz).urls == parse_sitemap(URLSET).urls


def test_bounds_and_bombs():
    big = b"<urlset>" + b"".join(b"<url><loc>https://a.test/p%d</loc></url>" % i for i in range(500)) + b"</urlset>"
    sm = parse_sitemap(big, max_urls=100)
    assert len(sm.urls) == 100 and sm.truncated
    bomb = gzip.compress(b"<urlset>" + b"<url><loc>https://a.test/x</loc></url>" * 200000 + b"</urlset>")
    sm2 = parse_sitemap(bomb, max_bytes=100_000)
    assert sm2.truncated and len(sm2.urls) >= 1


def test_xxe_and_garbage_safe():
    xxe = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>
<urlset><url><loc>https://a.test/&e;</loc></url><url><loc>https://a.test/ok</loc></url></urlset>"""
    sm = parse_sitemap(xxe)
    assert not any("root:" in u for u in sm.urls)
    for junk in [b"", b"\x00\x01", b"<urlset><url><loc>https://a.test/a</loc></url", b"<<<>>>", b"\x1f\x8bnotgzip"]:
        parse_sitemap(junk)  # never raises


def test_plain_text_sitemap():
    sm = parse_sitemap(b"https://a.test/contact\nhttps://a.test/about\n\nnot a url\nhttps://a.test/contact\n")
    assert sm.urls == ["https://a.test/contact", "https://a.test/about"]
