"""Regression tests for verified correctness bugs (yield counting, exports, merge, queue ownership, supervision, run lifecycle)."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app import db as dbmod
from app import jobqueue as jq
from app import runs
from app.crawler.types import EmailObservation
from app.exports import E, ExportFilters, _from, create_export, filter_conditions
from app.models import EmailAddress, Organisation, OrganisationDomain, SourcingRun, User
from app.pipeline import flow
from app.pipeline.ingest import persist_observations
from app.run_config import RunConfig
from app.sources import cc_persist

DOM = "acmewidgets.co.uk"
T0 = datetime(2019, 1, 1, tzinfo=UTC)


async def _org(s, name="Acme Widgets Ltd", ident=None, domain=DOM):
    org = Organisation(legal_name=name, country_code="GB", canonical_identity=ident or f"GB:nm:{name}")
    s.add(org)
    await s.flush()
    dom = OrganisationDomain(organisation_id=org.id, domain=domain, registrable_domain=domain, verified_match_status="confirmed",
                             confidence=0.95, evidence=[], parked=False)
    s.add(dom)
    await s.flush()
    return org, dom


async def _run(s, status="running", **target) -> int:
    cfg = RunConfig(name="r", sources=["csv_import"], target={"exclude_free_providers": True, **target})
    run = await runs.create_run(s, cfg, user_id=None, status=status)
    return run.id


async def _looks_verified(s, eid):
    await s.execute(text("UPDATE email_addresses SET overall_reachability='safe', confidence=80, syntax_status='valid', "
                         "domain_mail_status='accepts_mail' WHERE id=:i"), {"i": eid})


async def _published_ids(s, **flt):
    conds = await filter_conditions(s, ExportFilters(**flt))
    return {r[0] for r in (await s.execute(select(E.id).select_from(_from()).where(*conds))).all()}


# --------------------------------------------------------------------------- 1. archive + permutation is not "published"
async def test_archive_sighting_of_a_permutation_guess_is_not_published(session):
    """Finding 1 (repro_a): a role guess plus a Common Crawl sighting of the same address must not count / export as published."""
    s = session
    org, dom = await _org(s)
    rid = await _run(s)
    created = await flow.research_permutations(s, org, dom, rid, role_limit=3)
    assert created
    eid = created[0]
    em = (await s.execute(select(EmailAddress.email_normalised).where(EmailAddress.id == eid))).scalar_one()
    res = await cc_persist.persist_historical_observations(s, org.id, [EmailObservation(email=em, method="mailto", page_url=f"https://{DOM}/contact")],
                                                          archive_timestamp=T0, run_id=rid)
    assert [r.unconfirmed for r in res] == [True]  # a permutation guess is not live evidence
    e = (await s.execute(select(EmailAddress).where(EmailAddress.id == eid).execution_options(populate_existing=True))).scalar_one()
    assert e.origin == "role_speculative" and e.needs_review  # an archive sighting never upgrades a guess
    await _looks_verified(s, eid)
    assert await runs.record_yield(s, rid, eid) is False
    assert eid not in await _published_ids(s, published_only=True)
    # nothing to verify either: it rests on archive + speculative evidence only
    await s.execute(text("UPDATE email_addresses SET last_validated_at=NULL"))
    queued = await flow.enqueue_unvalidated_verifies(s, rid, org.id)
    keys = set((await s.execute(text("SELECT dedupe_key FROM sourcing_jobs WHERE run_id=:r AND stage='verify'"), {"r": rid})).scalars())
    assert f"email:{eid}" not in keys and queued == len(created) - 1

    # legacy row: stored origin 'published' but only permutation evidence -> still not published
    other = created[1]
    await s.execute(text("UPDATE email_addresses SET origin='published' WHERE id=:i"), {"i": other})
    await _looks_verified(s, other)
    assert await runs.record_yield(s, rid, other) is False
    assert other not in await _published_ids(s, published_only=True)

    # a LIVE crawl re-observing the archived address does confirm it
    await cc_persist.confirm_against_live(s, org.id, dom, [EmailObservation(email=em, method="mailto", page_url=f"https://{DOM}/team")], run_id=rid)
    e = (await s.execute(select(EmailAddress).where(EmailAddress.id == eid).execution_options(populate_existing=True))).scalar_one()
    assert e.origin == "published"
    assert await runs.record_yield(s, rid, eid) is True
    assert eid in await _published_ids(s, published_only=True)


async def test_archive_timestamps_and_confirmation_are_not_reset(session):
    """Finding 20: archive-only addresses get the capture time as first/last seen; a re-archive never bumps last_seen of a live
    address nor resets a live confirmation."""
    s = session
    org, dom = await _org(s)
    em = f"sales@{DOM}"
    await cc_persist.persist_historical_observations(s, org.id, [EmailObservation(email=em, method="mailto", page_url=f"https://{DOM}/c")], archive_timestamp=T0)
    e = (await s.execute(select(EmailAddress).where(EmailAddress.email_normalised == em))).scalar_one()
    assert e.first_seen_at == T0 and e.last_seen_at == T0
    now = datetime(2026, 10, 1, tzinfo=UTC)
    await cc_persist.confirm_against_live(s, org.id, dom, [EmailObservation(email=em, method="mailto", page_url=f"https://{DOM}/c2")], now=now)
    assert await cc_persist.unconfirmed_emails(s, org.id) == []
    # the same archive page is seen again (another crawl id): the live confirmation stays, last_seen stays at the live sighting
    res = await cc_persist.persist_historical_observations(s, org.id, [EmailObservation(email=em, method="mailto", page_url=f"https://{DOM}/c")],
                                                          archive_timestamp=datetime(2020, 1, 1, tzinfo=UTC), crawl_id="CC-2020")
    assert [r.unconfirmed for r in res] == [False]
    assert await cc_persist.unconfirmed_emails(s, org.id) == []
    e = (await s.execute(select(EmailAddress).where(EmailAddress.email_normalised == em).execution_options(populate_existing=True))).scalar_one()
    assert e.last_seen_at == now and e.first_seen_at == T0


# --------------------------------------------------------------------------- 3 / 19. run-scoped exports
async def _user(s) -> User:
    u = User(email="exp@example.com", password_hash="x", role="admin")
    s.add(u)
    await s.flush()
    return u


async def test_run_export_contains_only_that_runs_contacts(session):
    """Finding 3: a run's export is filtered by that run (it used to export the whole database); a mismatching filter is refused.
    Finding 19: an address first recorded by run A and re-observed + counted by run B is in BOTH runs' exports."""
    s = session
    u = await _user(s)
    org, dom = await _org(s)
    ra, rb = await _run(s), await _run(s)
    a = await persist_observations(s, org.id, dom, [EmailObservation(email=f"a@{DOM}", method="mailto", page_url=f"https://{DOM}/a")], run_id=ra)
    b = await persist_observations(s, org.id, dom, [EmailObservation(email=f"b@{DOM}", method="mailto", page_url=f"https://{DOM}/b")], run_id=rb)
    # run B re-observes a@ (same page + method): contact_evidence keeps run A, run B gets it through its yield
    await persist_observations(s, org.id, dom, [EmailObservation(email=f"a@{DOM}", method="mailto", page_url=f"https://{DOM}/a")], run_id=rb)
    owner = (await s.execute(text("SELECT run_id FROM contact_evidence WHERE email_id=:e"), {"e": a[0][0]})).scalars().all()
    assert owner == [ra]
    await _looks_verified(s, a[0][0])
    assert await runs.record_yield(s, rb, a[0][0]) is True
    await s.commit()

    ex_a = await create_export(s, user=u, preset="full_intelligence", fmt="jsonl", filters=ExportFilters(), run_id=ra, run_inline=True)
    ex_b = await create_export(s, user=u, preset="full_intelligence", fmt="jsonl", filters=ExportFilters(), run_id=rb, run_inline=True)
    assert ex_a.status == "done" and ex_a.row_count == 1 and ex_a.filters.get("run_id") == ra
    assert ex_b.status == "done" and ex_b.row_count == 2
    ids_b = await _published_ids(s, run_id=rb)
    assert ids_b == {a[0][0], b[0][0]}
    from app.security import ApiError

    with pytest.raises(ApiError):
        await create_export(s, user=u, preset="full_intelligence", fmt="csv", filters=ExportFilters(run_id=ra), run_id=rb, run_inline=True)


# --------------------------------------------------------------------------- 7. definition semantics
async def test_published_definition_never_counts_inferred(session):
    s = session
    with pytest.raises(ValueError, match="distinct_published_emails"):
        RunConfig(name="r", sources=["csv_import"], target={"published_only": False, "allow_inferred": True})
    snap = RunConfig(name="r", sources=["csv_import"]).model_dump(mode="json")
    snap["target"].update(published_only=False, allow_inferred=True)
    assert RunConfig.from_snapshot(snap).target.allow_inferred  # stored snapshots stay loadable
    org, dom = await _org(s)
    run = SourcingRun(name="r", status="running", configuration_snapshot=snap, target_count=10, target_definition="distinct_published_emails",
                      uniqueness="email")
    s.add(run)
    await s.flush()
    (eid,) = (await flow.research_permutations(s, org, dom, run.id, role_limit=1))
    await _looks_verified(s, eid)
    assert await runs.record_yield(s, run.id, eid) is False
    # the same inferred address does count for a definition that admits inferred candidates
    rid2 = await _run(s, definition="validated_contacts", published_only=False, allow_inferred=True)
    assert await runs.record_yield(s, rid2, eid) is True


# --------------------------------------------------------------------------- 9. per-company cap race
async def test_per_company_cap_holds_under_concurrency(session):
    """Finding 9 (repro_cap): two workers counting different addresses of the same company concurrently respect the cap."""
    s = session
    org, _dom = await _org(s)
    ids = []
    for loc in ("a", "b", "c"):
        e = EmailAddress(organisation_id=org.id, email_original=f"{loc}@{DOM}", email_normalised=f"{loc}@{DOM}", local_part=loc, domain=DOM,
                         registrable_domain=DOM, source_type="mailto", origin="published", confidence=80, syntax_status="valid",
                         overall_reachability="safe")
        s.add(e)
        await s.flush()
        ids.append(e.id)
    rid = await _run(s, max_contacts_per_company=1)
    await s.commit()

    async def count(eid):
        async with dbmod.sessionmaker()() as x:
            got = await runs.record_yield(x, rid, eid)
            await asyncio.sleep(0.05)  # keep the transaction open while the others try
            await x.commit()
            return got

    got = await asyncio.gather(*(count(i) for i in ids))
    n = (await s.execute(text("SELECT count(*) FROM run_yield WHERE run_id=:r"), {"r": rid})).scalar_one()
    assert n == 1 and sum(got) == 1


# --------------------------------------------------------------------------- 10. merge re-keys run_yield
async def test_merge_rekeys_run_yield_and_split_restores(session):
    """Finding 10 (repro_merge): after merging two companies counted separately, the run counts ONE company."""
    from app.discovery.resolution import merge_organisations, split_organisation

    s = session
    o1, _ = await _org(s, "Acme Ltd", "GB:nm:acme1")
    o2, _ = await _org(s, "ACME Limited", "GB:nm:acme2", domain="acme-two.co.uk")
    rid = await _run(s, uniqueness="organisation", count=10)
    rp = await _run(s, definition="business_email_pairs", count=10)
    eids = []
    for oid, loc in ((o1.id, "a"), (o2.id, "b")):
        e = EmailAddress(organisation_id=oid, email_original=f"{loc}@{DOM}", email_normalised=f"{loc}@{DOM}", local_part=loc, domain=DOM,
                         registrable_domain=DOM, source_type="mailto", origin="published", confidence=80, syntax_status="valid",
                         overall_reachability="safe")
        s.add(e)
        await s.flush()
        eids.append(e.id)
        assert await runs.record_yield(s, rid, e.id) is True
        assert await runs.record_yield(s, rp, e.id) is True
    await merge_organisations(s, o1.id, o2.id, None, "dup")
    keys = dict((await s.execute(text("SELECT unique_key, organisation_id FROM run_yield WHERE run_id=:r"), {"r": rid})).all())
    assert keys == {f"org:{o1.id}": o1.id}
    assert (await runs.compute_counters(s, rid))["qualified"] == 1
    pairs = set((await s.execute(text("SELECT unique_key FROM run_yield WHERE run_id=:r"), {"r": rp})).scalars())
    assert pairs == {f"pair:{o1.id}:a@{DOM}", f"pair:{o1.id}:b@{DOM}"}
    await split_organisation(s, o2.id, None, "undo")
    pairs = set((await s.execute(text("SELECT unique_key FROM run_yield WHERE run_id=:r"), {"r": rp})).scalars())
    assert pairs == {f"pair:{o1.id}:a@{DOM}", f"pair:{o2.id}:b@{DOM}"}


# --------------------------------------------------------------------------- 13. stale workers
async def test_stale_worker_cannot_settle_a_job_another_worker_holds(session):
    s = session
    rid = await _run(s)
    await s.commit()
    await jq.enqueue(s, run_id=rid, stage="crawl", dedupe_key="x")
    await s.commit()
    (job,) = await jq.claim(s, "w1", ["crawl"], 1, 60)
    await s.execute(text("UPDATE sourcing_jobs SET leased_until = now() - interval '1 second'"))
    await s.commit()
    await jq.reap_expired(s, 4)
    (job2,) = await jq.claim(s, "w2", ["crawl"], 1, 60)
    assert job2.id == job.id
    assert await jq.complete(s, job.id, worker_id="w1") is False
    assert await jq.requeue(s, job.id, 1.0, worker_id="w1") is False
    assert await jq.fail(s, job, RuntimeError("x"), retryable=False, max_attempts=4, worker_id="w1") == "ignored"
    await s.commit()
    row = (await s.execute(text("SELECT state, worker_id FROM sourcing_jobs WHERE id=:i"), {"i": job.id})).one()
    assert tuple(row) == ("leased", "w2")
    assert await jq.complete(s, job.id, worker_id="w2") is True
    await s.commit()
    assert (await s.execute(text("SELECT state FROM sourcing_jobs WHERE id=:i"), {"i": job.id})).scalar_one() == "done"


# --------------------------------------------------------------------------- 15. per-stage claim limits
async def test_claim_respects_free_stage_capacity(session):
    s = session
    rid = await _run(s)
    await s.commit()
    for i in range(5):
        await jq.enqueue(s, run_id=rid, stage="verify", dedupe_key=f"v{i}", priority=1)
        await jq.enqueue(s, run_id=rid, stage="crawl", dedupe_key=f"c{i}", priority=50)
    await s.commit()
    jobs = await jq.claim(s, "w", ["verify", "crawl", "discover"], 10, 60, stage_limits={"verify": 2, "crawl": 1, "discover": 0})
    assert sorted(j.stage for j in jobs) == ["crawl", "verify", "verify"]
    assert await jq.claim(s, "w", ["verify"], 10, 60, stage_limits={"verify": 0}) == []
    jobs = await jq.claim(s, "w", ["verify", "crawl"], 2, 60, stage_limits={"verify": 5, "crawl": 5})
    assert [j.stage for j in jobs] == ["verify", "verify"]  # overall limit + priority order still apply


# --------------------------------------------------------------------------- 17. no work for stopped runs
async def test_enqueue_refused_for_closed_runs_and_leftovers_cancelled(session):
    from app.system_runs import ensure_system_run

    s = session
    rid = await _run(s)
    await s.commit()
    assert await jq.enqueue(s, run_id=rid, stage="crawl", dedupe_key="a")
    await runs.stop_run(s, rid, "target_reached")
    await s.flush()
    assert await jq.enqueue(s, run_id=rid, stage="crawl", dedupe_key="b") is None  # in-flight job of a stopping run: refused
    # a child that raced the status change is swept when the run completes
    await s.execute(text("INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, priority, state) VALUES (:r,'verify','late','{}'::jsonb,1,'queued')"),
                    {"r": rid})
    assert await runs.evaluate_run(s, rid) == "completed"
    st = dict((await s.execute(text("SELECT dedupe_key, state FROM sourcing_jobs WHERE run_id=:r"), {"r": rid})).all())
    assert st == {"a": "cancelled", "late": "cancelled"}
    sys_rid = await ensure_system_run(s, "system-export")
    assert await jq.enqueue(s, run_id=sys_rid, stage="export", dedupe_key="e1")


# --------------------------------------------------------------------------- 21. active runtime / per-run attempts
async def test_max_runtime_ignores_paused_time(session):
    s = session
    rid = await _run(s, max_runtime_minutes=10, count=1000)
    run = await s.get(SourcingRun, rid)
    run.started_at = datetime.now(UTC) - timedelta(minutes=30)
    run.paused_at = datetime.now(UTC) - timedelta(minutes=25)
    run.status = "paused"
    await s.flush()
    await jq.enqueue(s, run_id=rid, stage="crawl", dedupe_key="keep")
    await runs.resume_run(s, rid)
    assert 1400 <= runs.paused_seconds(run.counters) <= 1600 and "companies" in run.counters
    assert await runs.evaluate_run(s, rid) is None and run.status == "running"  # 5 active minutes < 10
    assert runs.paused_seconds(run.counters) >= 1400  # evaluate_run kept the lifecycle bookkeeping
    run.counters = {**run.counters, "lifecycle": {"paused_seconds": 0}}
    await s.flush()
    assert await runs.evaluate_run(s, rid) in ("stopping", "completed") and run.stop_reason == "max_runtime"


async def test_retry_attempts_follow_the_run_setting(session):
    from app.worker import Worker

    s = session
    rid = await _run(s)
    snap = dict((await s.get(SourcingRun, rid)).configuration_snapshot)
    snap["performance"] = {**snap["performance"], "retry_max_attempts": 2}
    await s.execute(text("UPDATE sourcing_runs SET configuration_snapshot=CAST(:c AS jsonb) WHERE id=:r"), {"c": __import__("json").dumps(snap), "r": rid})
    await s.commit()
    w = Worker(concurrency=1, stages=["crawl"])
    assert await w.max_attempts_for(rid) == 2 and await w.max_attempts_for(None) == w.settings.max_job_attempts
    await jq.enqueue(s, run_id=rid, stage="crawl", dedupe_key="x")
    await s.commit()
    await jq.claim(s, "w1", ["crawl"], 1, 60)
    await s.execute(text("UPDATE sourcing_jobs SET leased_until = now() - interval '1 second', attempts=2"))
    await s.commit()
    await jq.reap_expired(s, 4)  # worker default 4, run says 2 -> dead-lettered
    assert (await s.execute(text("SELECT state FROM sourcing_jobs"))).scalar_one() == "dead"


# --------------------------------------------------------------------------- 14 / 22. supervisor
async def test_supervisor_holds_leadership_for_the_tick_and_isolates_runs(session, monkeypatch):
    from app.worker import Worker

    s = session
    bad, good = await _run(s), await _run(s)
    await s.commit()
    seen: dict[str, object] = {}
    orig = runs.evaluate_run

    async def evaluate(x, rid):
        async with dbmod.sessionmaker()() as other:
            seen.setdefault("other_leader", (await other.execute(text("SELECT pg_try_advisory_lock(727001)"))).scalar())
            # no run row stays locked across the tick: while one run is evaluated, the others are free (API pause/stop not blocked)
            other_id = good if rid == bad else bad
            locked = (await other.execute(text("SELECT id FROM sourcing_runs WHERE id=:r FOR UPDATE SKIP LOCKED"), {"r": other_id})).first()
            seen.setdefault(f"free_{rid}", locked is not None)
            await other.rollback()
        if rid == bad:
            raise RuntimeError("broken run")
        return await orig(x, rid)

    monkeypatch.setattr(runs, "evaluate_run", evaluate)
    w = Worker(concurrency=1, stages=["crawl"])
    assert await w.supervisor_tick() is True
    assert seen["other_leader"] is False  # leadership survived reap_expired's commit
    assert seen[f"free_{good}"] is True
    async with dbmod.sessionmaker()() as x:
        st = dict((await x.execute(text("SELECT id, status FROM sourcing_runs"))).all())
        assert st[good] == "completed" and st[bad] == "running"  # the broken run did not abort the others
        assert (await x.execute(text("SELECT pg_try_advisory_lock(727001)"))).scalar() is True  # released after the tick
        await x.execute(text("SELECT pg_advisory_unlock(727001)"))


# --------------------------------------------------------------------------- 16. job time limits
async def test_job_wall_clock_limit_and_lease_cutoff(session, monkeypatch):
    from app import worker as wmod
    from app.worker import Worker

    s = session
    rid = await _run(s)
    await s.commit()

    async def slow(ctx):
        await asyncio.sleep(5)

    monkeypatch.setitem(wmod.HANDLERS, "slowstage", slow)
    w = Worker(concurrency=1, stages=["slowstage"])
    monkeypatch.setattr(w, "STAGE_TIMEOUT_S", {"slowstage": 0.2})
    await jq.enqueue(s, run_id=rid, stage="slowstage", dedupe_key="x")
    await s.commit()
    (job,) = await jq.claim(s, w.worker_id, ["slowstage"], 1, 60)
    t0 = time.monotonic()
    await w._run_job(job)
    assert time.monotonic() - t0 < 3
    row = (await s.execute(text("SELECT state, error_code FROM sourcing_jobs WHERE id=:i"), {"i": job.id})).one()
    assert tuple(row) == ("queued", "job_timeout")
    # leases of jobs past their limit are no longer extended
    fresh, old = SimpleNamespace(job_id=1, job_stage="slowstage", job_started=time.monotonic()), \
        SimpleNamespace(job_id=2, job_stage="slowstage", job_started=time.monotonic() - 3600)
    w.tasks = [fresh, old]  # type: ignore[assignment]
    assert w.lease_ids() == [1]
    # streaming handlers get a cooperative soft budget
    ctx = wmod.JobContext(job=job, session=s, worker=w, deadline=time.monotonic() - 1)
    assert ctx.should_yield() and not wmod.JobContext(job=job, session=s, worker=w).should_yield()


# --------------------------------------------------------------------------- 5. discover: commit before the final checkpoint
async def test_discover_commits_tail_before_final_checkpoint(session, monkeypatch):
    from app.pipeline import handlers
    from app.sources.base import BusinessRecord, get_adapter
    from app.worker import JobContext

    s = session
    rid = await _run(s)
    await s.commit()
    real = get_adapter("csv_import", None)

    class Fake:
        info = real.info

        async def iter_businesses(self, query, cp):
            for i in range(3):
                yield BusinessRecord(source_ref=f"r{i}", legal_name=f"Tail Co {i} Ltd", country_code="GB", adapter_id="csv_import"), {"i": i}

    monkeypatch.setattr(handlers, "get_adapter", lambda a, p=None: Fake())
    committed_at_checkpoint: list[tuple[dict, int]] = []

    async def checkpoint(self, data):
        async with dbmod.sessionmaker()() as x:
            n = (await x.execute(text("SELECT count(*) FROM organisations"))).scalar_one()
        committed_at_checkpoint.append((data, n))

    monkeypatch.setattr(JobContext, "checkpoint", checkpoint)
    jid = await jq.enqueue(s, run_id=rid, stage="discover", dedupe_key="adapter:csv_import", payload={"adapter_id": "csv_import"})
    await s.commit()
    job = jq.Job(id=jid, run_id=rid, stage="discover", dedupe_key="adapter:csv_import", payload={"adapter_id": "csv_import"}, checkpoint={}, attempts=1)
    worker = SimpleNamespace(rss_mb=lambda: 1.0, run_status_is_running=lambda r: asyncio.sleep(0, True))
    async with dbmod.sessionmaker()() as hs:
        await handlers.discover(JobContext(job=job, session=hs, worker=worker))
        await hs.commit()
    final = [n for d, n in committed_at_checkpoint if d.get("finished")]
    assert final == [3]  # the tail batch was committed before the 'finished' checkpoint was written
