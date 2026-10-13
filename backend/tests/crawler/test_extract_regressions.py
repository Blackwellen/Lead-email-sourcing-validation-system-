"""Regressions from the security / correctness review."""
import time

from app.crawler.extract import extract_from_html, find_company_numbers, valid_email_parts


def test_company_number_regex_has_no_catastrophic_backtracking():
    for bait in ("company no", "registered in england", "registered in england and wales,", "Company Registration No."):
        t0 = time.perf_counter()
        find_company_numbers(bait + " " * 50_000 + "x")
        find_company_numbers(bait + " \n\t" * 20_000 + "x")
        assert time.perf_counter() - t0 < 1.0, bait


def test_company_numbers_still_found_across_whitespace_and_newlines():
    assert find_company_numbers("Registered in England and Wales\n  No.\t 01234567") == ["01234567"]
    assert find_company_numbers("Company   Registration   Number:  SC123456") == ["SC123456"]
    assert find_company_numbers("registered in Scotland, under company number 765432") == ["765432"]


def test_legal_edge_characters_are_not_stripped():
    assert valid_email_parts("jo_", "acme.co.uk") == "jo_@acme.co.uk"
    assert valid_email_parts("_sales", "acme.co.uk") == "_sales@acme.co.uk"
    assert valid_email_parts("-desk", "acme.co.uk") == "-desk@acme.co.uk"
    assert valid_email_parts("jean'pierre", "acme.fr") == "jean'pierre@acme.fr"


def test_only_known_french_elisions_are_removed():
    assert valid_email_parts("l'info", "acme.fr") == "info@acme.fr"
    assert valid_email_parts("qu'contact", "acme.fr") == "contact@acme.fr"
    found = {o.email for o in extract_from_html("<p>Ecrivez a l'info@acme.fr.</p>", "https://acme.fr/").emails}
    assert found == {"info@acme.fr"}
