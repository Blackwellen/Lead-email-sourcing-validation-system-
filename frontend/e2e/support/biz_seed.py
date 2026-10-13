"""Seed SYNTHETIC rows for the biz e2e suite (all example.com / .example domains; none of this is real data).

Idempotent: wipes previously seeded rows (canonical_identity LIKE 'e2e|%') first. Run via biz-backend.sh seed.
"""
import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db import sessionmaker

COUNTRIES = [("GB", ["Greater London", "West Midlands", "Scotland"]), ("IE", ["Cork", "Dublin"]), ("DE", ["Bavaria", "Berlin"]), ("FR", ["Occitanie"])]
DOMAIN_STATUS = ["confirmed", "confirmed", "high", "review", "mismatch", "unknown"]
REACH = ["safe", "safe", "risky", "unknown", "invalid"]
NOW = datetime.now(UTC)


async def main() -> None:
    rnd = random.Random(7)
    async with sessionmaker()() as s:
        await s.execute(text("DELETE FROM email_addresses WHERE organisation_id IN (SELECT id FROM organisations WHERE canonical_identity LIKE 'e2e|%')"))
        await s.execute(text("DELETE FROM organisation_domains WHERE organisation_id IN (SELECT id FROM organisations WHERE canonical_identity LIKE 'e2e|%')"))
        await s.execute(text("DELETE FROM contacts WHERE organisation_id IN (SELECT id FROM organisations WHERE canonical_identity LIKE 'e2e|%')"))
        await s.execute(text("DELETE FROM organisations WHERE canonical_identity LIKE 'e2e|%'"))
        cats = [r[0] for r in (await s.execute(text("SELECT id FROM categories WHERE level='subcategory' ORDER BY id LIMIT 6"))).all()]
        # ---- 260 synthetic businesses (more than the 200 max page size)
        for i in range(1, 261):
            cc, regions = COUNTRIES[i % len(COUNTRIES)]
            name = f"Synthetic Business {i:03d} Ltd"
            oid = (await s.execute(text(
                """INSERT INTO organisations (legal_name, trading_name, country_code, region, city, canonical_identity, discovery_source, category_id, legal_type, company_number,
                       sic_codes, last_checked_at, status)
                   VALUES (:n, :t, :c, :r, :ci, :ident, :src, :cat, :lt, :cn, '[]'::jsonb, :lc, 'active') RETURNING id"""),
                {"n": name, "t": f"Synth Trading {i:03d}" if i % 3 == 0 else None, "c": cc, "r": regions[i % len(regions)], "ci": f"City {i % 9}", "ident": f"e2e|{i}",
                 "src": ["companies_house_bulk", "osm_geofabrik", "csv_import"][i % 3], "cat": cats[i % len(cats)] if cats and i % 7 else None,
                 "lt": ["ltd", "plc", "sole trader"][i % 3], "cn": f"E2E{i:06d}", "lc": NOW - timedelta(days=i % 40) if i % 4 else None})).scalar_one()
            if i % 9 != 0:
                st = DOMAIN_STATUS[i % len(DOMAIN_STATUS)]
                dom = f"synthetic-{i:03d}.example.com"
                await s.execute(text(
                    """INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, website_url, verified_match_status, confidence, evidence, parked, manual_override)
                       VALUES (:o, :d, :d, :u, :st, :cf, CAST(:ev AS jsonb), false, false)"""),
                    {"o": oid, "d": dom, "u": f"https://{dom}", "st": st, "cf": {"confirmed": 0.95, "high": 0.8, "review": 0.45, "mismatch": 0.1, "unknown": 0.3}[st],
                     "ev": json.dumps([{"type": "name_match", "score": 0.9, "at": NOW.isoformat()}])})
                if i % 5 != 0:
                    for k in range(1 + i % 2):
                        origin = ["published", "corroborated", "pattern_inferred", "role_speculative"][(i + k) % 4]
                        reach = REACH[(i + k) % len(REACH)]
                        em = f"{['info','sales','contact'][k % 3] if origin == 'role_speculative' else f'person{i}{k}'}@{dom}"
                        await s.execute(text(
                            """INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, role_type,
                                   confidence, confidence_breakdown, overall_reachability, marketing_status, catch_all_status, domain_mail_status, syntax_status, recipient_evidence_status,
                                   last_validated_at, is_free_provider, is_disposable, needs_review, previously_bounced)
                               VALUES (:o, :e, :e, :l, :d, :d, 'website', :og, :rt, :cf, '{}'::jsonb, :re, 'needs_review', :ca, 'accepts_mail', 'valid', :rec, :v, false, false, false, false)"""),
                            {"o": oid, "e": em, "l": em.split("@")[0], "d": dom, "og": origin, "rt": "generic" if origin == "role_speculative" else None, "cf": float(30 + (i * 7 + k * 13) % 65),
                             "re": reach, "ca": ["untested", "suspected", "rejected_unknown_recipient"][(i + k) % 3], "rec": ["untested", "inconclusive", "smtp_recipient_accepted"][(i + k) % 3],
                             "v": None if (i + k) % 6 == 0 else NOW - timedelta(days=(i * 3) % 90)})
        # ---- XSS probe: hostile business name must render inertly
        xss = (await s.execute(text(
            """INSERT INTO organisations (legal_name, country_code, region, city, canonical_identity, discovery_source, legal_type, sic_codes)
               VALUES ('XSS <img src=x onerror="window.__xss=1"> <script>window.__xss=1</script> Ltd', 'GB', 'Greater London', 'London', 'e2e|xss', 'csv_import', 'ltd', '[]'::jsonb) RETURNING id"""))).scalar_one()
        await s.execute(text(
            """INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, confidence, confidence_breakdown,
                   overall_reachability, marketing_status, syntax_status, domain_mail_status, last_validated_at, is_free_provider, is_disposable, needs_review, previously_bounced, recipient_evidence_status, catch_all_status)
               VALUES (:o, 'xss@xss-probe.example.com', 'xss@xss-probe.example.com', 'xss', 'xss-probe.example.com', 'xss-probe.example.com', 'website', 'published', 55, '{}'::jsonb, 'risky', 'needs_review', 'valid', 'accepts_mail', now(), false, false, false, false, 'untested', 'untested')"""), {"o": xss})

        # ---- detailed "showcase" business with full evidence
        cat = cats[0] if cats else None
        acme = (await s.execute(text(
            """INSERT INTO organisations (legal_name, trading_name, country_code, region, city, postal_code, phone, canonical_identity, discovery_source, category_id, legal_type, company_number, industry, sic_codes, status, last_checked_at)
               VALUES ('Acme Synthetic Plumbing Ltd', 'Acme Synthetic', 'GB', 'Greater London', 'London', 'EX1 0AA', '+44 0000 000000', 'e2e|acme', 'companies_house_bulk', :c, 'ltd', 'E2EACME01', 'Plumbing', '[]'::jsonb, 'active', now()) RETURNING id"""), {"c": cat})).scalar_one()
        dup = (await s.execute(text(
            """INSERT INTO organisations (legal_name, country_code, region, city, canonical_identity, discovery_source, legal_type, sic_codes)
               VALUES ('Acme Synthetic Plumbing Limited', 'GB', 'Greater London', 'London', 'e2e|acme-dup', 'csv_import', 'ltd', '[]'::jsonb) RETURNING id"""))).scalar_one()
        did = (await s.execute(text(
            """INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, website_url, verified_match_status, confidence, evidence, parked, manual_override, last_checked_at)
               VALUES (:o, 'acme-synthetic.example.com', 'acme-synthetic.example.com', 'https://acme-synthetic.example.com', 'review', 0.55, CAST(:ev AS jsonb), false, false, now()) RETURNING id"""),
            {"o": acme, "ev": json.dumps([{"type": "name_match", "score": 0.7, "at": NOW.isoformat()}, {"type": "address_on_page", "match": True, "at": NOW.isoformat()}])})).scalar_one()
        await s.execute(text(
            """INSERT INTO organisation_domains (organisation_id, domain, registrable_domain, website_url, verified_match_status, confidence, evidence, parked, manual_override)
               VALUES (:o, 'acme-synthetic-old.example.org', 'acme-synthetic-old.example.org', NULL, 'mismatch', 0.1, '[]'::jsonb, true, false)"""), {"o": acme})
        jane = (await s.execute(text(
            """INSERT INTO contacts (organisation_id, full_name, given_name, family_name, job_title, department, identity_confidence, identity_key)
               VALUES (:o, 'Jane Synthetic', 'Jane', 'Synthetic', 'Managing Director', 'Management', 0.82, 'jane synthetic') RETURNING id"""), {"o": acme})).scalar_one()
        breakdown = {
            "score": 78, "reachability": "safe", "disclaimer": "'safe' means no adverse evidence was found for a published/corroborated address on a domain that accepts mail. It is not a guarantee of delivery or inbox placement.",
            "components": [
                {"component": "syntax", "points": 5, "max": 5, "note": "syntax valid"},
                {"component": "source_quality", "points": 22.5, "max": 25, "note": "source kind 'own_domain_page', reliability 0.90"},
                {"component": "identity", "points": 9.8, "max": 15, "note": "organisation/domain identity confidence 0.65"},
                {"component": "corroboration", "points": 10.6, "max": 15, "note": "contact confidence 0.82, 2 source(s)"},
                {"component": "domain_mail", "points": 15, "max": 15, "note": "domain mail status: accepts_mail"},
                {"component": "freshness", "points": 10, "max": 10, "note": "observed 12d ago"},
                {"component": "smtp_evidence", "points": 0, "max": 15, "note": "SMTP evidence: untested (acceptance is a diagnostic, not delivery)"},
                {"component": "catch_all_penalty", "points": -6, "max": 0, "note": "catch-all suspected from passive evidence"},
            ],
            "caps_applied": ["catch_all_suspected_max_70"], "reasons": ["catch-all suspected: acceptance proves nothing about this mailbox"],
        }
        jid = (await s.execute(text(
            """INSERT INTO email_addresses (organisation_id, contact_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, confidence, confidence_breakdown,
                   overall_reachability, marketing_status, catch_all_status, domain_mail_status, syntax_status, recipient_evidence_status, last_validated_at, last_seen_at, is_free_provider, is_disposable, needs_review, previously_bounced)
               VALUES (:o, :c, 'Jane.Synthetic@acme-synthetic.example.com', 'jane.synthetic@acme-synthetic.example.com', 'jane.synthetic', 'acme-synthetic.example.com', 'acme-synthetic.example.com',
                       'website', 'corroborated', 70, CAST(:bd AS jsonb), 'risky', 'needs_review', 'suspected', 'accepts_mail', 'valid', 'untested', now() - interval '3 days', now() - interval '12 days', false, false, false, false) RETURNING id"""),
            {"o": acme, "c": jane, "bd": json.dumps(breakdown)})).scalar_one()
        info = (await s.execute(text(
            """INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, role_type, confidence, confidence_breakdown,
                   overall_reachability, marketing_status, catch_all_status, domain_mail_status, syntax_status, recipient_evidence_status, last_validated_at, is_free_provider, is_disposable, needs_review, previously_bounced)
               VALUES (:o, 'info@acme-synthetic.example.com', 'info@acme-synthetic.example.com', 'info', 'acme-synthetic.example.com', 'acme-synthetic.example.com', 'permutation', 'role_speculative', 'generic', 40, '{}'::jsonb,
                       'unknown', 'needs_review', 'untested', 'accepts_mail', 'valid', 'untested', NULL, false, false, false, false) RETURNING id"""), {"o": acme})).scalar_one()
        for url, adapter, etype, rel in [("https://acme-synthetic.example.com/contact", "web_crawl", "page", 0.9), ("https://directory.example.org/listing/acme", "directory_dataset", "directory", 0.7), ("javascript:alert(1)", "csv_import", "dataset", 0.5)]:
            ev = (await s.execute(text(
                """INSERT INTO evidence_sources (source_adapter, source_url, evidence_type, observed_at, parser_version, source_reliability, licence)
                   VALUES (:a, :u, :t, now() - interval '12 days', 'e2e-1', :r, 'synthetic') RETURNING id"""), {"a": adapter, "u": url, "t": etype, "r": rel})).scalar_one()
            await s.execute(text("INSERT INTO contact_evidence (email_id, contact_id, organisation_id, evidence_id, extraction_method, context, observed_at) VALUES (:e, :c, :o, :ev, 'mailto_link', CAST(:cx AS jsonb), now() - interval '12 days')"),
                            {"e": jid, "c": jane, "o": acme, "ev": ev, "cx": json.dumps({"snippet": "Contact Jane Synthetic"})})
        vr = [("V01_syntax", "valid", None, {}, None, None), ("V02_domain", "resolves", None, {}, None, None), ("V03_mx", "accepts_mail", "mx_found", {"mx": ["mx1.example.com"]}, None, None),
              ("V05_flags", "clean", None, {}, None, None), ("V06_catchall", "suspected", "accept_all_gateway_provider", {"note": "passive evidence only"}, None, None),
              ("V07_smtp", "untested", "not_authorised_for_probe", {"note": "no SMTP connection was made"}, None, None), ("V10_delivery", "none", None, {}, None, None)]
        for ct, res, reason, detail, rc, esc in vr:
            await s.execute(text(
                """INSERT INTO validation_results (email_id, check_type, result, reason_code, detail, response_code, enhanced_status_code, observed_at, expires_at, verifier_version)
                   VALUES (:e, :ct, :r, :rs, CAST(:d AS jsonb), :rc, :esc, now() - interval '3 days', now() + interval '27 days', 'e2e-1')"""),
                {"e": jid, "ct": ct, "r": res, "rs": reason, "d": json.dumps(detail), "rc": rc, "esc": esc})
        await s.execute(text("INSERT INTO delivery_events (email_id, event_type, campaign, occurred_at, detail) VALUES (:e, 'delivered', 'synthetic-campaign', now() - interval '20 days', 'imported feedback (synthetic)')"), {"e": jid})
        run = (await s.execute(text(
            """INSERT INTO sourcing_runs (name, category_id, configuration_snapshot, target_count, target_definition, uniqueness, status, counters, archived, started_at, finished_at)
               VALUES ('E2E synthetic run', :c, '{}'::jsonb, 100, 'emails', 'organisation', 'completed', '{}'::jsonb, false, now() - interval '2 days', now() - interval '1 day') RETURNING id"""), {"c": cat})).scalar_one()
        await s.execute(text("INSERT INTO run_organisations (run_id, organisation_id, stage) VALUES (:r, :o, 'validated')"), {"r": run, "o": acme})
        await s.commit()
        print(json.dumps({"acme": acme, "dup": dup, "domain_id": did, "jane_email_id": jid, "info_email_id": info, "xss_org": xss, "run": run}))


asyncio.run(main())
