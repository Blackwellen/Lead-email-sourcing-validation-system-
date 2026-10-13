"""Live-QA regressions: stop exactly when the target is counted; verify leftovers after completion (never discard)."""
from sqlalchemy import text

from app import jobqueue as jq
from app import runs
from app.run_config import RunConfig


async def _setup(session, n_emails: int, target: int):
    cfg = RunConfig(name="t", countries=["GB"], imported_domains=["example.com"],
                    target={"count": target, "definition": "distinct_published_emails", "min_confidence": 0, "exclude_free_providers": False})
    run = await runs.create_run(session, cfg, user_id=None, status="running")
    org = (await session.execute(text("INSERT INTO organisations (legal_name, country_code, canonical_identity) VALUES ('X','GB','x') RETURNING id"))).scalar()
    ev = (await session.execute(text("INSERT INTO evidence_sources (source_adapter, source_url, evidence_type, content_hash) VALUES ('website_crawl','https://example.com/c','mailto','h') RETURNING id"))).scalar()
    ids = []
    for i in range(n_emails):
        e = (await session.execute(text(
            """INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type,
                 origin, confidence, syntax_status, overall_reachability) VALUES (:o, :e, :e, :l, 'example.com', 'example.com', 'website',
                 'published', 50, 'valid', 'unknown') RETURNING id"""), {"o": org, "e": f"p{i}@example.com", "l": f"p{i}"})).scalar()
        await session.execute(text("INSERT INTO contact_evidence (email_id, organisation_id, evidence_id, run_id, extraction_method) VALUES (:e,:o,:v,:r,'mailto')"),
                              {"e": e, "o": org, "v": ev, "r": run.id})
        ids.append(e)
    await session.commit()
    return run.id, ids


async def test_counting_the_target_stops_the_run_immediately(session):
    rid, ids = await _setup(session, 5, target=2)
    await jq.enqueue(session, run_id=rid, stage="verify", dedupe_key="pending", payload={"email_id": ids[4]})
    await session.commit()
    assert await runs.record_yield(session, rid, ids[0])
    assert (await session.execute(text("select status from sourcing_runs where id=:r"), {"r": rid})).scalar() == "running"
    assert await runs.record_yield(session, rid, ids[1])
    st = (await session.execute(text("select status, stop_reason from sourcing_runs where id=:r"), {"r": rid})).one()
    assert tuple(st) == ("stopping", "target_reached")
    assert (await session.execute(text("select state from sourcing_jobs where dedupe_key='pending'"))).scalar() == "cancelled"
    # a verify job already in flight may still count: nothing is discarded
    assert await runs.record_yield(session, rid, ids[2])


async def test_completion_queues_fast_verification_for_unverified_leftovers(session):
    rid, ids = await _setup(session, 3, target=100)
    await session.execute(text("UPDATE email_addresses SET last_validated_at=now() WHERE id=:e"), {"e": ids[0]})
    await session.commit()
    assert await runs.evaluate_run(session, rid) == "completed"
    await session.commit()
    rows = (await session.execute(text(
        "select j.payload, r.configuration_snapshot->>'system' from sourcing_jobs j join sourcing_runs r on r.id=j.run_id where j.stage='verify'"))).all()
    assert sorted(p["email_id"] for p, _ in rows) == ids[1:]
    assert all(p["no_yield"] and p["mode"] == "fast" and p["after_run"] == rid for p, _ in rows)
    assert all(sysflag for _, sysflag in rows)  # queued under the hidden system run, never the finished run
