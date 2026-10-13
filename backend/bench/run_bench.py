"""Measured benchmark suite. Every number printed is measured on the machine running it; nothing is hardcoded.

Usage (from backend/, against a DISPOSABLE database -- it is wiped):
    LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead_bench \
        python -m bench.run_bench --sizes 10000,100000,1000000 [--out bench/results]

Stages per size N:
  syntax      V01 syntax/normalisation checks per second (pure CPU)
  extract     synthetic HTML pages parsed per second (see bench_extract for details)
  ingest      persist_observations() through the real ingest path (DB upserts + provenance), capped at 20k
  fixture     bulk SQL fixture load of N email rows (generate_series) -- measures PostgreSQL, labelled as such
  verify      FAST verification via verify_email() with an in-memory DNS resolver (no network), capped at 20k,
              reports DNS cache hit rate
  queue       enqueue + claim + complete N jobs (capped at 100k) with 4 concurrent workers
  api         keyset list latency first page / deep page, dashboard overview latency
  export      streaming CSV export of all N rows: rows/s and peak RSS of this process
DNS and HTTP are local/fake: these are processing-capacity numbers, NOT internet throughput.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import resource
import sys
import threading
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from sqlalchemy import text  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.models import Base  # noqa: E402

PROC = psutil.Process()


class PeakRSS:
    """Samples RSS every 20 ms in a background thread."""

    def __enter__(self):
        self.peak = PROC.memory_info().rss
        self.base = self.peak
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, PROC.memory_info().rss)
            time.sleep(0.02)

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()

    @property
    def peak_mb(self):
        return round(self.peak / 1e6, 1)

    @property
    def delta_mb(self):
        return round((self.peak - self.base) / 1e6, 1)


async def reset_schema():
    async with dbmod.get_engine().begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)


def bench_syntax(n: int) -> dict:
    from app.verify.syntax import check_syntax

    rng = random.Random(1)
    addrs = [f"{rng.choice(['info','sales','john.smith','a_b','ümlaut','x+tag'])}{i}@company{i % 5000}.co.uk" for i in range(n)]
    t0 = time.perf_counter()
    valid = sum(1 for a in addrs if check_syntax(a).status == "valid")
    dt = time.perf_counter() - t0
    return {"n": n, "seconds": round(dt, 3), "checks_per_s": round(n / dt), "valid": valid}


def bench_extract(pages: int) -> dict:
    from bench.bench_extract import make_page

    from app.crawler.extract import extract_from_html

    rng = random.Random(7)
    docs = [make_page(rng, rng.choice(["cards", "table", "text", "jsonld"]), rng.randint(2, 12), rng.randint(5, 25)) for _ in range(pages)]
    nbytes = sum(len(h) for h, _ in docs)
    t0 = time.perf_counter()
    found = sum(len({o.email for o in extract_from_html(h, "https://example.com/contact").emails}) for h, _ in docs)
    dt = time.perf_counter() - t0
    return {"pages": pages, "seconds": round(dt, 3), "pages_per_s": round(pages / dt, 1), "emails_per_s": round(found / dt),
            "mb_per_s": round(nbytes / 1e6 / dt, 2), "expected_emails": sum(n for _, n in docs), "found_emails": found}


async def bench_ingest(n: int) -> dict:
    from app.crawler.types import EmailObservation
    from app.models import Organisation, OrganisationDomain
    from app.pipeline.ingest import persist_observations

    n = min(n, 20_000)
    per_org = 5
    t0 = time.perf_counter()
    async with dbmod.sessionmaker()() as s:
        for o in range(n // per_org):
            org = Organisation(legal_name=f"Bench Org {o}", country_code="GB", canonical_identity=f"bench|{o}")
            s.add(org)
            await s.flush()
            dom = OrganisationDomain(organisation_id=org.id, domain=f"bench{o}.example.com", registrable_domain=f"bench{o}.example.com",
                                     website_url=f"https://bench{o}.example.com", verified_match_status="confirmed", confidence=0.95)
            s.add(dom)
            await s.flush()
            obs = [EmailObservation(email=f"person{i}@bench{o}.example.com", method="mailto", page_url=f"https://bench{o}.example.com/contact",
                                    name=f"Person {i} Bench", title="Director") for i in range(per_org)]
            await persist_observations(s, org.id, dom, obs, run_id=None)
            if o % 200 == 0:
                await s.commit()
        await s.commit()
    dt = time.perf_counter() - t0
    return {"emails": n, "seconds": round(dt, 2), "emails_per_s": round(n / dt, 1),
            "note": "full provenance path: org+domain+email+contact+evidence rows per observation"}


async def bench_fixture(n: int) -> dict:
    t0 = time.perf_counter()
    async with dbmod.sessionmaker()() as s:
        orgs = max(1, n // 5)
        await s.execute(text("""INSERT INTO organisations (legal_name, country_code, region, city, canonical_identity, discovery_source)
            SELECT 'Fixture Org ' || g, 'GB', 'Region ' || (g % 9), 'City ' || (g % 50), 'fx|' || g, 'companies_house_bulk' FROM generate_series(1, :o) g"""), {"o": orgs})
        base = (await s.execute(text("SELECT min(id) FROM organisations WHERE canonical_identity LIKE 'fx|%'"))).scalar()
        await s.execute(text("""INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain,
              source_type, origin, confidence, overall_reachability, marketing_status, syntax_status, domain_mail_status, last_validated_at, role_type)
            SELECT :b + (g % :o), 'u' || g || '@fx' || (g % 9973) || '.example.com', 'u' || g || '@fx' || (g % 9973) || '.example.com', 'u' || g,
                   'fx' || (g % 9973) || '.example.com', 'fx' || (g % 9973) || '.example.com', 'website', 'published', 40 + (g % 60),
                   (ARRAY['safe','risky','unknown','invalid'])[1 + g % 4], 'needs_review', 'valid', 'accepts_mail', now(),
                   CASE WHEN g % 3 = 0 THEN 'info' END
            FROM generate_series(1, :n) g"""), {"n": n, "o": orgs, "b": base})
        await s.commit()
        await s.execute(text("ANALYZE organisations"))
        await s.execute(text("ANALYZE email_addresses"))
        await s.commit()
    dt = time.perf_counter() - t0
    return {"rows": n, "seconds": round(dt, 2), "rows_per_s": round(n / dt), "note": "SQL generate_series fixture load (PostgreSQL bulk insert speed)"}


async def bench_verify(n: int) -> dict:
    from verify.fakes import FakeResolver

    from app.verify import dnsmx
    from app.verify.pipeline import verify_email

    n = min(n, 20_000)
    res = FakeResolver()
    for d in range(9973):
        res.mail_domain(f"fx{d}.example.com", [(10, f"mx.fx{d}.example.com")])
    async with dbmod.sessionmaker()() as s:
        ids = (await s.execute(text("SELECT id FROM email_addresses ORDER BY id LIMIT :n"), {"n": n})).scalars().all()
    before = dict(dnsmx.CACHE_STATS)
    sem = asyncio.Semaphore(16)
    t0 = time.perf_counter()

    async def one(i):
        async with sem, dbmod.sessionmaker()() as s:
            await verify_email(s, i, mode="fast", resolver=res)
            await s.commit()

    await asyncio.gather(*(one(i) for i in ids))
    dt = time.perf_counter() - t0
    after = dnsmx.CACHE_STATS
    hits = after.get("hit", 0) - before.get("hit", 0)
    miss = after.get("miss", 0) - before.get("miss", 0) + after.get("expired", 0) - before.get("expired", 0)
    return {"emails": len(ids), "seconds": round(dt, 2), "emails_per_s": round(len(ids) / dt, 1), "dns_cache_hits": hits, "dns_cache_misses": miss,
            "dns_cache_hit_rate": round(hits / max(1, hits + miss), 3), "concurrency": 16, "resolver": "in-memory fake (no network)"}


async def bench_queue(n: int) -> dict:
    from app import jobqueue as jq
    from app.models import SourcingRun

    n = min(n, 100_000)
    async with dbmod.sessionmaker()() as s:
        r = SourcingRun(name="bench", status="running")
        s.add(r)
        await s.commit()
        rid = r.id
        t0 = time.perf_counter()
        await s.execute(text("""INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, checkpoint, priority, state, next_attempt_at, attempts)
                               SELECT :r, 'bench', 'k' || g, '{}'::jsonb, '{}'::jsonb, 100, 'queued', now(), 0 FROM generate_series(1, :n) g"""), {"r": rid, "n": n})
        await s.commit()
        t_enq = time.perf_counter() - t0
    done = 0

    async def worker(name):
        nonlocal done
        async with dbmod.sessionmaker()() as s:
            while True:
                jobs = await jq.claim(s, name, ["bench"], 100, 60)
                if not jobs:
                    return
                for j in jobs:
                    await jq.complete(s, j.id)
                await s.commit()
                done += len(jobs)

    t0 = time.perf_counter()
    await asyncio.gather(*(worker(f"w{i}") for i in range(4)))
    dt = time.perf_counter() - t0
    return {"jobs": n, "bulk_enqueue_s": round(t_enq, 2), "claim_complete_s": round(dt, 2), "jobs_per_s": round(done / dt), "workers": 4,
            "note": "queue overhead only (no-op handlers): claim batch 100 with FOR UPDATE SKIP LOCKED"}


async def bench_api() -> dict:
    import httpx
    from api.helpers import make_admin_client

    from app.main import create_app

    out = {}
    app = create_app()
    async with make_admin_client(app) as c:
        async def timed(path, reps=5):
            ts = []
            body = None
            for _ in range(reps):
                t0 = time.perf_counter()
                r = await c.get(path)
                ts.append(time.perf_counter() - t0)
                assert r.status_code == 200, (path, r.status_code, r.text[:300])
                body = r.json()
            ts.sort()
            return round(ts[len(ts) // 2] * 1000, 1), body

        out["contacts_first_page_ms_p50"], first = await timed("/api/contacts?limit=100")
        cursor = first.get("next_cursor")
        pages = 0
        t0 = time.perf_counter()
        while cursor and pages < 50:  # walk 50 pages deep: keyset cost must stay flat
            r = await c.get("/api/contacts", params={"limit": 100, "cursor": cursor})
            cursor = r.json().get("next_cursor")
            pages += 1
        out["contacts_deep_page_ms_avg"] = round((time.perf_counter() - t0) / max(1, pages) * 1000, 1)
        out["contacts_filtered_ms_p50"], _ = await timed("/api/contacts?limit=100&reachability=safe&min_confidence=70")
        out["businesses_first_page_ms_p50"], _ = await timed("/api/businesses?limit=100")
        out["dashboard_overview_ms_p50"], ov = await timed("/api/dashboard/overview", reps=3)
        out["dashboard_approximate"] = bool(ov.get("approximate")) if isinstance(ov, dict) else None
    _ = httpx
    return out


async def bench_export() -> dict:
    from api.helpers import make_admin_client  # noqa: F401  (ensures helpers import side effects are fine)

    from app import exports
    from app.exports import ExportFilters
    from app.models import User

    async with dbmod.sessionmaker()() as s:
        u = User(email=f"bench{time.time_ns()}@example.com", password_hash="x", role="admin")
        s.add(u)
        await s.commit()
        with PeakRSS() as m:
            t0 = time.perf_counter()
            ex = await exports.create_export(s, user=u, preset="full_intelligence", fmt="csv", filters=ExportFilters(), run_inline=True)
            dt = time.perf_counter() - t0
        size = ex.file_size or 0
        return {"rows": ex.row_count, "seconds": round(dt, 2), "rows_per_s": round((ex.row_count or 0) / dt), "file_mb": round(size / 1e6, 1),
                "process_peak_rss_mb": m.peak_mb, "rss_growth_during_export_mb": m.delta_mb, "status": ex.status}


async def run(sizes: list[int], out_dir: Path) -> dict:
    results = {"machine": {"python": platform.python_version(), "cpus": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
                           "platform": platform.platform()}, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sizes": {}}
    results["syntax"] = bench_syntax(200_000)
    results["extract"] = bench_extract(300)
    for n in sizes:
        print(f"== size {n:,}", flush=True)
        await reset_schema()
        r: dict = {}
        r["ingest"] = await bench_ingest(n)
        print("  ingest", r["ingest"], flush=True)
        await reset_schema()
        r["fixture"] = await bench_fixture(n)
        print("  fixture", r["fixture"], flush=True)
        r["verify"] = await bench_verify(n)
        print("  verify", r["verify"], flush=True)
        r["api"] = await bench_api()
        print("  api", r["api"], flush=True)
        r["export"] = await bench_export()
        print("  export", r["export"], flush=True)
        r["queue"] = await bench_queue(n)
        print("  queue", r["queue"], flush=True)
        results["sizes"][str(n)] = r
    results["peak_rss_mb_process"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="10000,100000")
    ap.add_argument("--out", default="bench/results")
    a = ap.parse_args()
    url = os.environ.get("LEAD_DATABASE_URL", "")
    if not url.rstrip("/").split("/")[-1].startswith("lead_bench"):
        sys.exit("refusing to run: LEAD_DATABASE_URL must point at a disposable database whose name starts with 'lead_bench'")
    os.environ.setdefault("LEAD_EXPORT_DIR", "/tmp/lead-bench/exports")
    os.environ.setdefault("LEAD_DATA_DIR", "/tmp/lead-bench")
    os.environ.setdefault("LEAD_RATE_LIMIT_PER_MINUTE", "100000")
    res = asyncio.run(run([int(x) for x in a.sizes.split(",")], Path(a.out)))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
