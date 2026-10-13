"""Benchmark the e-mail extractor on generated synthetic pages (measured, never hardcoded).

Usage:  python -m bench.bench_extract [--pages 400] [--repeat 3]   (from the backend directory)
"""
from __future__ import annotations

import argparse
import random
import resource
import statistics
import time

from app.crawler.extract import extract_from_html

FIRST = ["John", "Mary", "Ahmed", "Chloe", "Wei", "Priya", "Lars", "Sofia", "Tom", "Aisha", "Pierre", "Anna"]
LAST = ["Smith", "Jones", "Patel", "Nguyen", "Garcia", "O'Neil", "van Dijk", "Müller", "Brown", "Khan", "Rossi", "Lee"]
ROLES = ["Managing Director", "Senior Solicitor", "Office Manager", "Surveyor", "Partner", "Accountant", "Head of Sales"]


def make_page(rng: random.Random, kind: str, n_people: int, filler_paragraphs: int) -> tuple[str, int]:
    """-> (html, number of distinct e-mail addresses published)"""
    dom = f"company{rng.randrange(10_000)}.co.uk"
    parts = [f"<!doctype html><html lang='en'><head><title>Company {dom}</title>"
             "<meta name='description' content='synthetic page'><script>var x=1;</script><style>p{margin:0}</style></head><body>"
             "<nav>" + "".join(f"<a href='/p{i}'>Page {i}</a>" for i in range(30)) + "<a href='/contact-us'>Contact</a></nav>"]
    n_emails = 0
    for _ in range(filler_paragraphs):
        parts.append("<p>" + " ".join(rng.choice(["lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "service", "quality"]) for _ in range(60)) + "</p>")
    if kind == "cards":
        parts.append("<section class='team'>")
        for i in range(n_people):
            f, ln = rng.choice(FIRST), rng.choice(LAST)
            parts.append(f"<div class='team-member'><h3>{f} {ln}</h3><p>{rng.choice(ROLES)}</p>"
                         f"<a href='mailto:{f.lower()}{i}@{dom}'>Email</a><a href='tel:+44207946{i:04d}'>call</a></div>")
            n_emails += 1
        parts.append("</section>")
    elif kind == "table":
        parts.append("<table><thead><tr><th>Name</th><th>Position</th><th>Email</th></tr></thead>")
        for i in range(n_people):
            f, ln = rng.choice(FIRST), rng.choice(LAST)
            parts.append(f"<tr><td>{f} {ln}</td><td>{rng.choice(ROLES)}</td><td>{f.lower()}{i}@{dom}</td></tr>")
            n_emails += 1
        parts.append("</table>")
    elif kind == "obfuscated":
        for i in range(n_people):
            parts.append(f"<p>Contact user{i} [at] {dom.split('.')[0]} [dot] co [dot] uk or info{i}@{dom}</p>")
            n_emails += 2
    else:  # text
        for i in range(n_people):
            parts.append(f"<p>Email us: office{i}@{dom} for enquiries about order #{rng.randrange(10**6)}.</p>")
            n_emails += 1
    parts.append(f"<footer>Registered in England and Wales No. {rng.randrange(10**7, 10**8)}. Tel: 020 7946 0000</footer></body></html>")
    return "".join(parts), n_emails


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=400)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    kinds = ["cards", "table", "obfuscated", "text", "text"]
    pages = [make_page(rng, kinds[i % len(kinds)], n_people=rng.randrange(1, 25), filler_paragraphs=rng.choice([3, 20, 120]))
             for i in range(a.pages)]
    total_bytes = sum(len(p[0].encode()) for p in pages)
    expected = sum(p[1] for p in pages)
    extract_from_html(pages[0][0], "https://x.test/")  # warm-up (imports, TLD table)
    rates = []
    found = 0
    for _ in range(a.repeat):
        found = 0
        t = time.perf_counter()
        for html, _n in pages:
            found += len(extract_from_html(html, "https://x.test/").emails)
        dt = time.perf_counter() - t
        rates.append((len(pages) / dt, found / dt, total_bytes / dt / 1e6))
    pps = statistics.median(r[0] for r in rates)
    eps = statistics.median(r[1] for r in rates)
    mbps = statistics.median(r[2] for r in rates)
    print(f"pages: {len(pages)}  total html: {total_bytes / 1e6:.2f} MB (avg {total_bytes / len(pages) / 1024:.1f} KiB/page)")
    print(f"emails published (ground truth): {expected}   extracted: {found}   recall: {found / expected:.3f}")
    print(f"throughput (median of {a.repeat} runs, single core): {pps:,.1f} pages/s  |  {eps:,.1f} emails/s  |  {mbps:.2f} MB/s")
    print(f"peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB")


if __name__ == "__main__":
    main()
