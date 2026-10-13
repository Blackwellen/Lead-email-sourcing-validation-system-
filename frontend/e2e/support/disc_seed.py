"""Seed SYNTHETIC rows for the disc-*.spec.ts e2e suite (DB lead_fe_d). Every name/domain is invented (example.com only).

Idempotent: deletes previously seeded rows (canonical_identity LIKE 'e2e-d|%' and their dependants) first, then recreates:
  * "Disc Hub Holdings Ltd" with a confirmed website, 12 related businesses (every relation type) and a depth-2 ring;
  * "Disc Mega Group Ltd" with 320 'member_of_group' members (graph truncation at the 300-node cap);
  * 15 software lookalikes of the hub (same SIC / category / region) for the similar-business finder;
  * 40 business signals (4 types, 3 countries) incl. one hostile `javascript:` source URL (must render as inert text);
  * stored completeness rows (overview distribution / opportunities) and a "Disc Completeness Probe Ltd" whose LIVE
    completeness has all four states (yes / no / in progress / unknown);
  * data-quality issues: duplicate + unresolved review links, unconfirmed domains, stale + catch-all e-mails, a contact without
    e-mail, an organisation with two confirmed websites (conflicting evidence);
  * source-effectiveness counters: one adapter with enough samples (ratios computed) and one below the minimum (ratios null);
  * one monitor and a draft run ("Disc e2e target run") for the similar-business "expand into run" action.
Run: cd backend && LEAD_DATABASE_URL=... PYTHONPATH=. ../.venv/bin/python ../frontend/e2e/support/disc_seed.py
"""
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text

from app.db import sessionmaker
from app.models import (
    BusinessRelationship,
    BusinessSignal,
    MonitorSchedule,
    OrganisationCompleteness,
    OrganisationRecordLink,
    SourceEffectiveness,
    SourcingJob,
    SourcingRun,
)

NOW = datetime.now(UTC)
TAG = "e2e-d|"
RELATIONS = ["branch_of", "brand_of", "subsidiary_of", "supplier_of", "partner_of", "franchise_of", "member_of_group", "exhibits_with", "awarded_by", "same_group"]
SIGNAL_TYPES = ["hiring", "expansion", "contract_award", "new_location"]


async def org(s, ident: str, name: str, *, cc: str = "GB", region: str = "Greater London", city: str = "London", industry: str | None = "Software",
              sic: list[str] | None = None, cat: int | None = None, last_checked: datetime | None = None, postal: str | None = None) -> int:
    return (await s.execute(text(
        """INSERT INTO organisations (legal_name, country_code, region, city, industry, canonical_identity, discovery_source, category_id, legal_type, sic_codes,
               last_checked_at, status, postal_code)
           VALUES (:n, :c, :r, :ci, :ind, :ident, 'csv_import', :cat, 'ltd', CAST(:sic AS jsonb), :lc, 'active', :pc) RETURNING id"""),
        {"n": name, "c": cc, "r": region, "ci": city, "ind": industry, "ident": TAG + ident, "cat": cat, "sic": json.dumps(sic or []), "lc": last_checked,
         "pc": postal})).scalar_one()


async def domain(s, oid: int, dom: str, status: str = "confirmed", *, crawled: bool = False, checked: bool = True) -> int:
    return (await s.execute(text(
        """INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, website_url, verified_match_status, confidence, evidence, parked,
               manual_override, last_checked_at, last_crawled_at)
           VALUES (:o, :d, :d, :u, :st, :cf, CAST(:ev AS jsonb), false, false, :lc, :cr) RETURNING id"""),
        {"o": oid, "d": dom, "u": f"https://{dom}", "st": status, "cf": {"confirmed": 0.95, "high": 0.8, "review": 0.45}.get(status, 0.3),
         "ev": json.dumps([{"type": "name_match", "score": 0.9, "at": NOW.isoformat()}]), "lc": NOW - timedelta(days=2) if checked else None,
         "cr": NOW - timedelta(days=1) if crawled else None})).scalar_one()


async def email(s, oid: int, addr: str, *, origin: str = "published", last_seen_days: int = 1, catch_all: str = "untested", reach: str = "safe",
                validated: bool = True, contact_id: int | None = None) -> int:
    dom = addr.split("@")[1]
    return (await s.execute(text(
        """INSERT INTO email_addresses (organisation_id, contact_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin,
               confidence, confidence_breakdown, overall_reachability, marketing_status, catch_all_status, domain_mail_status, syntax_status, recipient_evidence_status,
               last_validated_at, last_seen_at, is_free_provider, is_disposable, needs_review, previously_bounced)
           VALUES (:o, :c, :e, :e, :l, :d, :d, 'website', :og, 70, '{}'::jsonb, :re, 'needs_review', :ca, 'accepts_mail', 'valid', 'untested', :v, :ls,
               false, false, false, false) RETURNING id"""),
        {"o": oid, "c": contact_id, "e": addr, "l": addr.split("@")[0], "d": dom, "og": origin, "re": reach, "ca": catch_all,
         "v": NOW - timedelta(days=3) if validated else None, "ls": NOW - timedelta(days=last_seen_days)})).scalar_one()


async def wipe(s) -> None:
    ids = f"(SELECT id FROM organisations WHERE canonical_identity LIKE '{TAG}%')"
    await s.execute(text(f"UPDATE organisations SET merged_into_id = NULL WHERE canonical_identity LIKE '{TAG}%'"))
    await s.execute(text(f"DELETE FROM sourcing_jobs WHERE payload->>'seed' = 'e2e-d'"))
    await s.execute(text(f"DELETE FROM suppression_entries WHERE kind='email' AND value LIKE '%@disc-%.example.com'"))
    for t in ("business_signals", "organisation_completeness", "email_addresses", "organisation_domains", "contacts"):
        await s.execute(text(f"DELETE FROM {t} WHERE organisation_id IN {ids}"))
    await s.execute(text(f"DELETE FROM business_relationships WHERE from_org_id IN {ids} OR to_org_id IN {ids}"))
    await s.execute(text(f"DELETE FROM organisation_record_links WHERE organisation_id IN {ids} OR candidate_org_id IN {ids}"))
    await s.execute(text(f"DELETE FROM run_organisations WHERE organisation_id IN {ids}"))
    await s.execute(text(f"DELETE FROM organisations WHERE canonical_identity LIKE '{TAG}%'"))
    await s.execute(delete(SourceEffectiveness).where(SourceEffectiveness.adapter_id.in_(("companies_house_bulk", "osm_geofabrik"))))
    await s.execute(delete(MonitorSchedule).where(MonitorSchedule.name.like("Disc e2e%")))
    await s.execute(delete(SourcingRun).where(SourcingRun.name == "Disc e2e target run"))


async def main() -> None:
    async with sessionmaker()() as s:
        # Same migrated-DB workaround as biz_seed.py (sourcing_jobs.attempts lacks its server default after migration 0001).
        await s.execute(text("ALTER TABLE sourcing_jobs ALTER COLUMN attempts SET DEFAULT 0"))
        await wipe(s)
        cat = (await s.execute(text("SELECT id FROM categories WHERE level='subcategory' ORDER BY id LIMIT 1"))).scalar()

        # ---------------------------------------------------------------- network
        hub = await org(s, "hub", "Disc Hub Holdings Ltd", sic=["62012"], cat=cat, last_checked=NOW - timedelta(days=1), postal="EC1A 1BB")
        await domain(s, hub, "disc-hub.example.com", crawled=True)
        await email(s, hub, "hello@disc-hub.example.com")
        partners = []
        for i, rel in enumerate(RELATIONS + ["supplier_of", "partner_of"]):
            p = await org(s, f"p{i}", f"Disc Partner {i:02d} Ltd", cc=["GB", "IE", "DE"][i % 3], city=["Leeds", "Cork", "Berlin"][i % 3])
            if i % 2 == 0:
                await domain(s, p, f"disc-partner-{i:02d}.example.com")
            partners.append(p)
            fr, to = (p, hub) if i % 2 else (hub, p)
            s.add(BusinessRelationship(from_org_id=fr, to_org_id=to, relation=rel, source_adapter="website_links", depth=1, confidence=round(0.95 - i * 0.04, 2),
                                       source_url=f"https://disc-hub.example.com/partners#{i}", observed_at=NOW - timedelta(days=i)))
        for j in range(6):  # depth-2 ring off the first partners
            q = await org(s, f"ring{j}", f"Disc Ring {j} Ltd", city="Manchester")
            s.add(BusinessRelationship(from_org_id=q, to_org_id=partners[j], relation="supplier_of", source_adapter="website_links", depth=2, confidence=0.6,
                                       source_url=f"https://disc-partner-{j:02d}.example.com/suppliers", observed_at=NOW - timedelta(days=3)))
        mega = await org(s, "mega", "Disc Mega Group Ltd", industry="Holding")
        for k in range(320):
            m = (await s.execute(text(
                "INSERT INTO organisations (legal_name, country_code, canonical_identity, discovery_source, legal_type, sic_codes, status) "
                "VALUES (:n, 'GB', :i, 'csv_import', 'ltd', '[]'::jsonb, 'active') RETURNING id"), {"n": f"Disc Member {k:03d} Ltd", "i": f"{TAG}m{k}"})).scalar_one()
            s.add(BusinessRelationship(from_org_id=m, to_org_id=mega, relation="member_of_group", source_adapter="register_groups", depth=1,
                                       confidence=round(0.5 + (k % 50) / 100, 2), source_url="https://registry.example.com/groups/mega", observed_at=NOW - timedelta(hours=k)))

        # ---------------------------------------------------------------- lookalikes
        for i in range(15):
            await org(s, f"look{i}", f"Disc Software Lookalike {i:02d} Ltd", sic=["62012"] if i % 3 else ["62020"], cat=cat, postal=f"EC{1 + i % 3}A 2BB",
                      industry="Software")

        # ---------------------------------------------------------------- signals
        sig_orgs = partners[:8]
        for i in range(40):
            o = sig_orgs[i % len(sig_orgs)]
            st = SIGNAL_TYPES[i % 4]
            url = f"https://news.example.com/disc/{i}" if i != 7 else "javascript:alert(document.domain)"
            title = f"Disc signal {i:02d}: {st.replace('_', ' ')}" + (" (hostile link probe)" if i == 7 else "")
            s.add(BusinessSignal(organisation_id=o, signal_type=st, title=title, detail={"seed": True}, source_adapter="news_feed", source_url=url,
                                 occurred_at=NOW - timedelta(days=i + 2) if i % 3 else None, observed_at=NOW - timedelta(days=i),
                                 confidence=round(0.3 + (i % 7) / 10, 2), dedupe_hash=hashlib.sha256(f"e2e-d-{i}".encode()).hexdigest()))

        # ---------------------------------------------------------------- completeness (stored rows: overview)
        states = ["yes", "no", "unknown", "in_progress"]
        for i, p in enumerate(partners + [hub]):
            pe = states[i % 4]
            s.add(OrganisationCompleteness(organisation_id=p, official_website="yes" if i % 2 == 0 else states[(i + 1) % 4], published_email=pe,
                                           contact_page_inspected=states[(i + 2) % 4], score=10 + i * 6))
        # probe org: live completeness yes/no/in_progress/unknown
        probe = await org(s, "probe", "Disc Completeness Probe Ltd", last_checked=None)
        await domain(s, probe, "disc-probe.example.com", crawled=True)
        pe = await email(s, probe, "guess@disc-probe.example.com", origin="pattern_inferred", validated=False)
        run = SourcingRun(name="Disc e2e target run", status="draft", configuration_snapshot={}, target_count=50)
        s.add(run)
        await s.flush()
        s.add(SourcingJob(run_id=run.id, stage="verify", dedupe_key=f"e2e-d:verify:{pe}", payload={"email_id": pe, "seed": "e2e-d"}, state="queued", attempts=0))

        # ---------------------------------------------------------------- data quality
        dup_a = await org(s, "dupA", "Disc Duplicate Alpha Ltd")
        dup_b = await org(s, "dupB", "Disc Duplicate Alpha Limited")
        dup_c = await org(s, "dupC", "Disc Twin Beta Ltd")
        dup_d = await org(s, "dupD", "Disc Twin Beta Limited")
        for a, b, ref in ((dup_a, dup_b, "dupAB"), (dup_c, dup_d, "dupCD")):
            s.add(OrganisationRecordLink(organisation_id=a, candidate_org_id=b, source_adapter="companies_house_bulk", source_ref=ref, match_basis="name_geo",
                                         match_confidence=0.82, status="review", observed_at=NOW - timedelta(days=1), detail={}))
        unresolved = await org(s, "unres", "Disc Unresolved Gamma Ltd")
        s.add(OrganisationRecordLink(organisation_id=unresolved, candidate_org_id=None, source_adapter="osm_geofabrik", source_ref="node/1", match_basis="name_geo",
                                     match_confidence=0.55, status="review", observed_at=NOW - timedelta(days=2), detail={}))
        for i in range(3):
            u = await org(s, f"udom{i}", f"Disc Unconfirmed Domain {i} Ltd")
            await domain(s, u, f"disc-unconfirmed-{i}.example.com", status="review")
        stale_org = await org(s, "stale", "Disc Stale Mail Ltd")
        await domain(s, stale_org, "disc-stale.example.com")
        for i in range(3):
            await email(s, stale_org, f"old{i}@disc-stale.example.com", last_seen_days=200 + i)
        ca_org = await org(s, "catchall", "Disc Catchall Ltd")
        await domain(s, ca_org, "disc-catchall.example.com")
        await email(s, ca_org, "any@disc-catchall.example.com", catch_all="confirmed", reach="risky")
        await s.execute(text("INSERT INTO contacts (organisation_id, full_name, given_name, family_name, job_title, identity_confidence, identity_key) "
                             "VALUES (:o, 'Dana Discovery', 'Dana', 'Discovery', 'Director', 0.8, 'e2e-d-dana')"), {"o": ca_org})
        conflict = await org(s, "conflict", "Disc Two Websites Ltd")
        await domain(s, conflict, "disc-two-a.example.com")
        await domain(s, conflict, "disc-two-b.example.com")

        # ---------------------------------------------------------------- effectiveness + monitors
        s.add(SourceEffectiveness(adapter_id="companies_house_bulk", run_id=None, records_seen=5000, unique_businesses=1200, duplicates=300, confirmed_domains=480,
                                  published_emails=260, eligible_contacts=140, http_requests=7000, failures=4, jobs=200, cpu_seconds=900.0, wall_seconds=7200.0,
                                  peak_rss_mb=410.0))
        s.add(SourceEffectiveness(adapter_id="osm_geofabrik", run_id=None, records_seen=4, unique_businesses=3, duplicates=0, confirmed_domains=1, published_emails=0,
                                  eligible_contacts=0, http_requests=0, failures=0, jobs=1))
        s.add(MonitorSchedule(kind="signals", name="Disc e2e seeded signals monitor", frequency="weekly", config={"limit": 20}, enabled=True, last_result={}))
        await s.commit()
        n = (await s.execute(select(text("count(*)")).select_from(text("organisations")).where(text(f"canonical_identity LIKE '{TAG}%'")))).scalar()
        print(json.dumps({"organisations": n, "hub": hub, "mega": mega, "probe": probe, "run": run.id}))


if __name__ == "__main__":
    asyncio.run(main())
