import pytest

from app.crawler.extract import (
    clean_email,
    decode_cfemail,
    extract_from_html,
    extract_from_pdf_bytes,
    extract_from_vcard,
    find_company_numbers,
    find_phones,
    looks_like_person_name,
    phone_key,
    rank_internal_links,
    scan_text_for_emails,
)
from tests.crawler.pdfgen import make_pdf

URL = "https://acme.co.uk/"


def cf(email: str, key: int = 0x3C) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


def emails(html, **kw):
    return {o.email: o for o in extract_from_html(html, URL, **kw).emails}


# ----------------------------------------------------------------------------- mailto / text / entities
def test_mailto_strips_query_and_handles_case_and_multiple():
    e = emails('<a href="MAILTO:Sales@Acme.co.uk?subject=Hi%20there&body=x">x</a>'
               '<a href="mailto:a@acme.co.uk,b@acme.co.uk">y</a><a href="mailto:%73ales2@acme.co.uk">z</a>')
    assert set(e) == {"Sales@acme.co.uk", "a@acme.co.uk", "b@acme.co.uk", "sales2@acme.co.uk"}
    assert all(o.method == "mailto" for o in e.values())


def test_visible_text_trailing_punctuation_and_entities():
    e = emails("<p>Write to info@acme.co.uk. Or (sales@acme.co.uk), or &lt;hr@acme.co.uk&gt; "
               "or help&#64;acme.co.uk; also support&commat;acme.co.uk!</p>")
    assert set(e) == {"info@acme.co.uk", "sales@acme.co.uk", "hr@acme.co.uk", "help@acme.co.uk", "support@acme.co.uk"}
    assert all(o.method == "text" for o in e.values())
    assert "info@acme.co.uk" in e["info@acme.co.uk"].context


def test_mailto_and_text_same_address_merge_with_also_methods():
    ex = extract_from_html('<a href="mailto:info@acme.co.uk">info@acme.co.uk</a>', URL)
    assert len(ex.emails) == 1
    assert ex.emails[0].method == "mailto" and ex.emails[0].also_methods == ("text",)


def test_idn_domain_email():
    e = emails("<p>kontakt@bücher.de</p>")
    assert "kontakt@bücher.de" in e


def test_url_userinfo_is_not_an_email():
    assert emails('<p>see https://user@host.example.com/path</p>') == {}


# ----------------------------------------------------------------------------- cloudflare
def test_cfemail_decoding_both_forms():
    assert decode_cfemail(cf("hello@acme.co.uk")) == "hello@acme.co.uk"
    assert decode_cfemail("zz") is None and decode_cfemail("") is None
    html = (f'<a href="/cdn-cgi/l/email-protection#{cf("one@acme.co.uk")}">[email&#160;protected]</a>'
            f'<span class="__cf_email__" data-cfemail="{cf("two@acme.co.uk", 0x11)}">[email&#160;protected]</span>')
    e = emails(html)
    assert set(e) == {"one@acme.co.uk", "two@acme.co.uk"}
    assert all(o.method == "obfuscated" for o in e.values())


# ----------------------------------------------------------------------------- obfuscations
@pytest.mark.parametrize("text,expected", [
    ("jsmith [at] acme-widgets [dot] com", "jsmith@acme-widgets.com"),
    ("john.smith (at) acme (dot) co (dot) uk", "john.smith@acme.co.uk"),
    ("sales {at} acme {dot} co.uk", "sales@acme.co.uk"),
    ("info[at]acme.co.uk", "info@acme.co.uk"),
    ("info [@] acme.co.uk", "info@acme.co.uk"),
    ("Email: info at acme dot co dot uk", "info@acme.co.uk"),
    ("sales at acme dot com", "sales@acme.com"),
    ("anna.lee at acme dot com", "anna.lee@acme.com"),
])
def test_obfuscation_positives(text, expected):
    got = {e: m for e, m, _ in scan_text_for_emails(text)}
    assert expected in got and got[expected] == "obfuscated"


@pytest.mark.parametrize("text", [
    "meet me at the park dot com tomorrow",
    "look at example dot com",
    "we arrive at 5 dot 30",
    "Opening times at acme dot",
    "stay at home dot org",
    "great at design and more",
    "contact at the office",
    "call us at 0800 123 456",
    "Join us at www.acme.co.uk",
    "price at 5.99 dot com",
])
def test_obfuscation_false_positive_guards(text):
    assert scan_text_for_emails(text) == []


def test_obfuscated_in_html_has_method():
    e = emails("<p>Mail us: info (at) acme (dot) co (dot) uk</p>")
    assert e["info@acme.co.uk"].method == "obfuscated"


# ----------------------------------------------------------------------------- garbage
@pytest.mark.parametrize("bad", [
    "logo@2x.png", "icon@3x.jpg", "sprite@2x.svg", "a@sentry.io", "abc123@sentry-next.wixpress.com",
    "0123456789abcdef0123456789abcdef@o123.ingest.sentry.io", "you@example.com", "test@example.org",
    "yourname@yourdomain.com", "name@domain.com", "user@localhost", "u003einfo@acme.co.uk", "x@y.z",
    "foo@bar.notatld", "a..b@acme.co.uk", "@acme.co.uk", "info@acme", "file@script.js", "email@email.com",
])
def test_garbage_filtered(bad):
    assert clean_email(bad) is None
    assert emails(f"<p>contact {bad} thanks</p>") == {}


def test_apostrophes_and_french_elision():
    assert clean_email("o'neil@acme.ie") == "o'neil@acme.ie"
    assert set(emails("<p>l'info@acme.co.uk et 'quoted@acme.co.uk' </p>")) == {"info@acme.co.uk", "quoted@acme.co.uk"}


def test_valid_addresses_survive_filter():
    for good in ["info@acme.co.uk", "a.b+tag@sub.acme.com", "o'neil@acme.ie", "x_y@acme.io", "hr@email.co.uk"]:
        assert clean_email(good), good


def test_hidden_honeypots_ignored():
    html = ('<span style="display: none">trap@acme.co.uk</span><div hidden>trap2@acme.co.uk</div>'
            '<p aria-hidden="true">trap3@acme.co.uk</p><a style="visibility:hidden" href="mailto:trap4@acme.co.uk">x</a>'
            '<p>real@acme.co.uk</p>')
    assert set(emails(html)) == {"real@acme.co.uk"}


def test_scripts_and_styles_not_scanned():
    html = '<script>var e="js@acme.co.uk"</script><style>/* css@acme.co.uk */</style><body><noscript>ns@acme.co.uk</noscript><p>ok@acme.co.uk</p></body>'
    assert set(emails(html)) == {"ok@acme.co.uk"}


# ----------------------------------------------------------------------------- JSON-LD / microdata
JSONLD = """
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"LocalBusiness","@id":"#org","name":"Acme Plumbing Ltd","legalName":"Acme Plumbing Limited",
  "url":"https://acme.co.uk","telephone":"+44 20 7946 0000","email":"mailto:office@acme.co.uk",
  "sameAs":["https://facebook.com/acme","https://www.linkedin.com/company/acme"],
  "address":{"@type":"PostalAddress","streetAddress":"1 High Street","addressLocality":"London","postalCode":"SW1A 1AA","addressCountry":"GB"},
  "vatID":"GB123456789",
  "contactPoint":[{"@type":"ContactPoint","contactType":"sales","email":"sales@acme.co.uk","telephone":"+44 20 7946 0001"}]},
 {"@type":"Person","name":"Jane Roe","jobTitle":"Managing Director","email":"jane@acme.co.uk"}
]}
</script>"""


def test_jsonld_graph_orgs_people_contactpoints():
    ex = extract_from_html(f"<html><head>{JSONLD}</head><body>x</body></html>", URL)
    e = {o.email: o for o in ex.emails}
    assert set(e) == {"office@acme.co.uk", "sales@acme.co.uk", "jane@acme.co.uk"}
    assert all(o.method == "jsonld" for o in e.values())
    assert e["jane@acme.co.uk"].name == "Jane Roe" and e["jane@acme.co.uk"].title == "Managing Director"
    assert e["sales@acme.co.uk"].department == "sales" and e["sales@acme.co.uk"].phone == "+44 20 7946 0001"
    assert e["office@acme.co.uk"].name is None  # organisation email is not a person
    org = next(o for o in ex.org if o.schema_type == "localbusiness")
    assert org.name == "Acme Plumbing Ltd" and org.legal_name == "Acme Plumbing Limited"
    assert org.postcode == "SW1A 1AA" and org.locality == "London" and org.vat_id == "GB123456789"
    assert org.telephone == ["+44 20 7946 0000"] and len(org.same_as) == 2 and org.url == "https://acme.co.uk"


def test_jsonld_malformed_tolerated():
    trailing = '<script type="application/ld+json">{"@type":"Organization","name":"X","email":"a@acme.co.uk",}</script>'
    assert "a@acme.co.uk" in emails(trailing)
    broken = '<script type="application/ld+json">{"@type":"Organization", "email": "b@acme.co.uk</script><p>c@acme.co.uk</p>'
    assert "c@acme.co.uk" in emails(broken)  # the rest of the page still works
    assert emails('<script type="application/ld+json"></script><script type="application/ld+json">[1,2,"x"]</script>') == {}
    cdata = '<script type="application/ld+json">//<![CDATA[\n{"@type":"Organization","email":"d@acme.co.uk"}\n//]]></script>'
    assert "d@acme.co.uk" in emails(cdata)


def test_jsonld_person_name_never_from_email_and_org_name_not_a_person():
    html = '<script type="application/ld+json">{"@type":"Person","name":"Acme Sales Team","email":"sales@acme.co.uk"}</script>'
    assert emails(html)["sales@acme.co.uk"].name is None


def test_microdata_itemprop_email():
    html = ('<div itemscope itemtype="https://schema.org/Person"><span itemprop="name">Sam Lee</span>'
            '<span itemprop="jobTitle">Partner</span><a itemprop="email" href="mailto:sam@acme.co.uk">mail</a>'
            '<span itemprop="telephone">020 7946 0099</span></div>')
    o = emails(html)["sam@acme.co.uk"]
    assert (o.name, o.title, o.phone) == ("Sam Lee", "Partner", "020 7946 0099")
    assert o.method == "jsonld" and o.context == "microdata"


# ----------------------------------------------------------------------------- vCard
VCF = """BEGIN:VCARD
VERSION:3.0
N:Smith;John;;Dr;
FN:Dr John Smith
ORG:Acme Ltd;Sales
TITLE:Head of Sales
TEL;TYPE=WORK,VOICE:+44 20 7946 0123
TEL;TYPE=FAX:+44 20 7946 0124
EMAIL;TYPE=INTERNET,WORK:john.smith@acme.co.uk
EMAIL;TYPE=HOME:jsmith@gmail.com
END:VCARD
BEGIN:VCARD
VERSION:2.1
FN:Acme Reception
EMAIL:reception@acme.co.uk
END:VCARD
"""


def test_vcard_text_and_inline_block():
    obs = extract_from_vcard(VCF, URL + "x.vcf")
    by = {o.email: o for o in obs}
    assert set(by) == {"john.smith@acme.co.uk", "jsmith@gmail.com", "reception@acme.co.uk"}
    j = by["john.smith@acme.co.uk"]
    assert j.method == "vcard" and j.name == "Dr John Smith" and j.title == "Head of Sales"
    assert j.department == "Sales" and j.phone == "+44 20 7946 0123"
    assert by["reception@acme.co.uk"].name is None  # "Acme Reception" is not a person
    # unfolded lines + garbage never crash
    assert extract_from_vcard("BEGIN:VCARD\nEMAIL:a@acme.co.uk\n x\nNONSENSE", URL) == []
    assert extract_from_vcard("", URL) == []
    html = f"<pre>{VCF}</pre>"
    assert "john.smith@acme.co.uk" in emails(html)


# ----------------------------------------------------------------------------- cards / tables / proximity
def test_team_cards_associate_name_title_phone():
    html = """
    <section class="team">
      <div class="team-member"><h3>John Smith</h3><p class="role">Managing Partner</p>
        <a href="mailto:john@acme.co.uk">Email John</a> <a href="tel:+442079460111">020 7946 0111</a></div>
      <div class="team-member"><h3>Maria Garcia Lopez</h3><span>Senior Solicitor</span>
        <a href="mailto:maria@acme.co.uk">maria@acme.co.uk</a></div>
      <div class="team-member"><h3>Ann O'Neil</h3><em>Office Manager</em><p>ann.oneil at acme dot co dot uk</p></div>
    </section>"""
    e = emails(html)
    assert (e["john@acme.co.uk"].name, e["john@acme.co.uk"].title) == ("John Smith", "Managing Partner")
    assert e["john@acme.co.uk"].phone == "+442079460111"
    assert (e["maria@acme.co.uk"].name, e["maria@acme.co.uk"].title) == ("Maria Garcia Lopez", "Senior Solicitor")
    assert e["maria@acme.co.uk"].phone is None  # John's phone is not Maria's
    assert e["ann.oneil@acme.co.uk"].name == "Ann O'Neil" and e["ann.oneil@acme.co.uk"].method == "obfuscated"


def test_table_with_header_and_headerless_rows():
    html = """<table><thead><tr><th>Name</th><th>Position</th><th>Department</th><th>Email</th><th>Phone</th></tr></thead>
    <tr><td>Bob Jones</td><td>Accountant</td><td>Finance</td><td><a href="mailto:bob@acme.co.uk">bob@acme.co.uk</a></td><td>Tel 020 7946 0200</td></tr>
    <tr><td>Cara Diaz</td><td>Surveyor</td><td>Valuations</td><td>cara@acme.co.uk</td><td></td></tr></table>
    <table><tr><td>Dan Wu</td><td>Director</td><td>dan@acme.co.uk</td></tr></table>"""
    e = emails(html)
    b = e["bob@acme.co.uk"]
    assert (b.name, b.title, b.department, b.method) == ("Bob Jones", "Accountant", "Finance", "table")
    assert b.phone and phone_key(b.phone) == phone_key("020 7946 0200")
    assert e["cara@acme.co.uk"].name == "Cara Diaz" and e["cara@acme.co.uk"].method == "table"
    assert e["dan@acme.co.uk"].name == "Dan Wu"


def test_definition_list_association():
    html = "<dl><dt>Eve Adams</dt><dd>Practice Manager</dd><dd><a href='mailto:eve@acme.co.uk'>mail</a></dd></dl>"
    o = emails(html)["eve@acme.co.uk"]
    assert o.name == "Eve Adams" and o.title == "Practice Manager" and o.method == "table"


def test_conservative_no_association_when_container_has_two_emails():
    html = "<div class='team-member'><h3>John Smith</h3><p>Director</p><a href='mailto:a@acme.co.uk'>a</a><a href='mailto:b@acme.co.uk'>b</a></div>"
    e = emails(html)
    assert e["a@acme.co.uk"].name is None and e["b@acme.co.uk"].name is None


def test_conservative_no_association_for_generic_headings():
    html = "<div class='contact'><h2>Get In Touch</h2><p>Email us at <a href='mailto:info@acme.co.uk'>info@acme.co.uk</a></p></div>"
    o = emails(html)["info@acme.co.uk"]
    assert o.name is None and o.title is None
    html2 = "<footer><h4>Opening Hours</h4><a href='mailto:info@acme.co.uk'>mail</a></footer>"
    assert emails(html2)["info@acme.co.uk"].name is None


def test_never_infers_name_from_local_part():
    html = "<p>Email john.smith@acme.co.uk</p><div class='card'><a href='mailto:mary.jones@acme.co.uk'>mail</a></div>"
    e = emails(html)
    assert e["john.smith@acme.co.uk"].name is None and e["mary.jones@acme.co.uk"].name is None


def test_department_label_before_address():
    o = emails("<p>Sales: sales@acme.co.uk | Accounts: accounts@acme.co.uk</p>")
    assert o["sales@acme.co.uk"].department == "Sales" and o["accounts@acme.co.uk"].department == "Accounts"


def test_looks_like_person_name():
    for ok in ["John Smith", "Dr. Anne-Marie O'Neil", "Ludwig van Beethoven", "María José García", "李 明 Wang"]:
        assert looks_like_person_name(ok), ok
    for bad in ["Contact Us", "Our Team", "Sales Team", "Head Office", "info@acme.com", "John", "Acme Ltd",
                "Managing Director", "Opening Hours", "Call 0800 123", "Get In Touch Today Friends Now"]:
        assert not looks_like_person_name(bad), bad


# ----------------------------------------------------------------------------- identity facts
def test_company_numbers_phones_postcodes_footer():
    html = """<html><head><title>Acme Ltd | Plumbers</title><meta property="og:site_name" content="Acme Plumbing">
    <meta name="robots" content="noindex,nofollow"><link rel="canonical" href="https://acme.co.uk/"></head>
    <body><h1>Acme Plumbing</h1><footer><p>&copy; 2026 Acme Plumbing Ltd. Registered in England and Wales No. 01234567.
    Registered office: 1 High St, London SW1A 1AA. Tel: 020 7946 0000. Fax: 020 7946 0001</p></footer></body></html>"""
    ex = extract_from_html(html, URL)
    assert ex.title == "Acme Ltd | Plumbers" and ex.h1 == ["Acme Plumbing"] and ex.site_name == "Acme Plumbing"
    assert ex.company_numbers == ["01234567"] and ex.postcodes == ["SW1A 1AA"]
    assert [phone_key(p) for p in ex.phones] == [phone_key("020 7946 0000")]  # fax excluded
    assert ex.noindex and ex.nofollow and ex.canonical == "https://acme.co.uk/"
    assert "Registered in England" in ex.footer_text


@pytest.mark.parametrize("text,expected", [
    ("Company No: 12345678", ["12345678"]),
    ("Company Number 07654321.", ["07654321"]),
    ("Registered in Scotland, company number SC123456", ["SC123456"]),
    ("Registered in England & Wales under company no. 1234567", ["1234567"]),
    ("Co. Reg. No. 00112233", ["00112233"]),
    ("Registered number: NI 612345", ["NI612345"]),
    ("Registered in England and Wales 09876543", ["09876543"]),
    ("Call 07700 900123 or visit us", []),
    ("Order number 12345678 shipped", []),
])
def test_company_number_patterns(text, expected):
    assert find_company_numbers(text) == expected


def test_find_phones():
    assert find_phones("Tel: 020 7946 0000 | Mobile +44 7700 900123") == ["020 7946 0000", "+44 7700 900123"] or len(find_phones("Tel: 020 7946 0000 | Mobile +44 7700 900123")) == 2
    assert find_phones("Fax: 020 7946 0001") == []
    assert find_phones("Established 1998-2004 and 2010 2020") == []
    assert phone_key("+44 (0)20 7946 0000") == phone_key("020 7946 0000")[-10:] or phone_key("+44 20 7946 0000") == phone_key("020 7946 0000")


def test_tel_links_and_org_phones():
    ex = extract_from_html('<a href="tel:+44 20 7946 0000">call</a><a href="tel:">x</a>', URL)
    assert ex.phones == ["+44 20 7946 0000"]


# ----------------------------------------------------------------------------- links
def test_rank_internal_links():
    html = """<nav><a href="/">Home</a><a href="/contact-us">Contact</a><a href="/about-us">About</a>
    <a href="/our-team">Meet the team</a><a href="/locations/leeds">Leeds branch</a><a href="/services/boilers">Boiler repair</a>
    <a href="/blog/2024/05/post">News</a><a href="/login">Login</a><a href="/brochure.pdf">Brochure</a>
    <a href="https://other.com/contact">Other</a><a href="/img/logo.png">logo</a><a href="/impressum">Impressum</a>
    <a href="/privacy-policy">Privacy</a><a href="mailto:a@acme.co.uk">mail</a><a href="javascript:void(0)">js</a><a href="#top">top</a></nav>"""
    ex = extract_from_html(html, URL)
    ranked = rank_internal_links(ex.links, URL, keywords=["boiler"])
    urls = [r.url for r in ranked]
    assert urls[0] == "https://acme.co.uk/contact-us"
    assert urls.index("https://acme.co.uk/our-team") < urls.index("https://acme.co.uk/about-us")
    assert urls.index("https://acme.co.uk/impressum") < urls.index("https://acme.co.uk/locations/leeds")
    assert "https://acme.co.uk/services/boilers" in urls
    assert not any(x in urls for x in ("https://acme.co.uk/login", "https://other.com/contact", "https://acme.co.uk/img/logo.png"))
    assert not any("blog" in u for u in urls)
    assert "https://acme.co.uk/brochure.pdf" in urls
    assert [r.reason for r in ranked if r.url.endswith("/contact-us")] == ["contact"]
    assert all(r.score > 0 for r in ranked)


def test_base_href_and_relative_links_resolved():
    ex = extract_from_html('<base href="https://acme.co.uk/sub/"><a href="contact">c</a>', "https://acme.co.uk/x")
    assert [lk.url for lk in ex.links] == ["https://acme.co.uk/sub/contact"]


# ----------------------------------------------------------------------------- robustness
def test_broken_html_never_raises():
    for html in ["", "<<<>>>", "<div><p>unclosed <a href=mailto:x@acme.co.uk>link", "\x00\x01\x02", "<html" * 1000,
                 "<a href='mailto:" + "a" * 10000 + "@acme.co.uk'>x</a>"]:
        ex = extract_from_html(html, URL)
        assert ex.page_url == URL
    assert "x@acme.co.uk" in emails("<div><p>unclosed <a href=mailto:x@acme.co.uk>link")


def test_company_hint_does_not_invent_data():
    ex = extract_from_html("<p>x@acme.co.uk</p>", URL, company_hint="Acme Ltd")
    assert [o.email for o in ex.emails] == ["x@acme.co.uk"]


# ----------------------------------------------------------------------------- PDF
def test_pdf_text_and_link_annotations():
    pdf = make_pdf(["Acme Brochure", "Contact: sales@acme.co.uk", "Hello (at) bracket test: ops [at] acme [dot] co [dot] uk",
                    "Registered in England and Wales No. 01234567"], mailto="annot@acme.co.uk", pages=2)
    ex = extract_from_pdf_bytes(pdf, "https://acme.co.uk/b.pdf")
    got = {o.email: o for o in ex.emails}
    assert {"sales@acme.co.uk", "annot@acme.co.uk", "ops@acme.co.uk"} <= set(got)
    assert all(o.method == "pdf" for o in got.values())
    assert ex.company_numbers == ["01234567"]


def test_pdf_page_bound_and_broken_inputs():
    pdf = make_pdf(["x"], pages=1)
    pages5 = make_pdf(["page-line"], pages=5)
    assert extract_from_pdf_bytes(pages5, "u", max_pages=2).text_length < extract_from_pdf_bytes(pages5, "u", max_pages=5).text_length
    for junk in [b"", b"not a pdf", pdf[:80], b"%PDF-1.4\n" + b"\x00" * 100, pdf.replace(b"xref", b"xxxx")]:
        assert extract_from_pdf_bytes(junk, "u").emails == []


def test_pdf_encrypted_not_bypassed():
    import io

    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["secret@acme.co.uk"]))))
    w.encrypt(user_password="hunter2")
    buf = io.BytesIO()
    w.write(buf)
    assert extract_from_pdf_bytes(buf.getvalue(), "u").emails == []
