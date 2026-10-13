"""Sourcing-run lifecycle, database-enforced yield counting and stop conditions."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import jobqueue as jq
from .models import SourcingRun
from .run_config import RunConfig

log = logging.getLogger("runs")

TERMINAL = {"completed", "failed", "cancelled"}


class RunStateError(Exception):
    pass


async def create_run(s: AsyncSession, cfg: RunConfig, *, user_id: int | None, status: str = "draft") -> SourcingRun:
    run = SourcingRun(
        name=cfg.name,
        category_id=cfg.category_ids[0] if cfg.category_ids else None,
        destination_category_id=cfg.destination_category_id,
        created_by=user_id,
        configuration_snapshot=cfg.model_dump(mode="json"),
        target_count=cfg.target.count,
        target_definition=cfg.target.definition.value,
        uniqueness=cfg.target.uniqueness.value,
        status=status,
        counters={},
    )
    s.add(run)
    await s.flush()
    return run


async def _get(s: AsyncSession, run_id: int, for_update: bool = True) -> SourcingRun:
    q = select(SourcingRun).where(SourcingRun.id == run_id)
    if for_update:
        q = q.with_for_update()
    run = (await s.execute(q)).scalar_one_or_none()
    if run is None:
        raise RunStateError("run not found")
    return run


async def start_run(s: AsyncSession, run_id: int) -> SourcingRun:
    run = await _get(s, run_id)
    if run.status not in ("draft", "scheduled", "failed", "queued"):
        raise RunStateError(f"cannot start a run in state {run.status}")
    cfg = RunConfig.from_snapshot(run.configuration_snapshot)
    run.status = "running"
    run.started_at = run.started_at or datetime.now(UTC)
    run.finished_at = None
    run.stop_reason = None
    await s.flush()  # jq.enqueue refuses work for runs that are not live: the new status must be visible to it
    await seed_initial_jobs(s, run, cfg)
    await s.flush()
    return run


async def seed_initial_jobs(s: AsyncSession, run: SourcingRun, cfg: RunConfig) -> None:
    """One 'discover' job per selected source adapter; domain imports become resolve/crawl jobs."""
    for i, adapter_id in enumerate(cfg.sources):
        await jq.enqueue(
            s, run_id=run.id, stage="discover", dedupe_key=f"adapter:{adapter_id}",
            payload={"adapter_id": adapter_id, "params": cfg.source_params.get(adapter_id, {})}, priority=10 + i,
        )
    for d in cfg.imported_domains:
        await jq.enqueue(s, run_id=run.id, stage="import_domain", dedupe_key=f"dom:{d.lower()}", payload={"domain": d.lower()}, priority=20)


async def pause_run(s: AsyncSession, run_id: int) -> SourcingRun:
    run = await _get(s, run_id)
    if run.status != "running":
        raise RunStateError(f"cannot pause a run in state {run.status}")
    run.status = "paused"
    run.paused_at = datetime.now(UTC)
    return run


async def resume_run(s: AsyncSession, run_id: int) -> SourcingRun:
    run = await _get(s, run_id)
    if run.status != "paused":
        raise RunStateError(f"cannot resume a run in state {run.status}")
    run.status = "running"
    await _account_paused(s, run)
    return run


def paused_seconds(counters: dict[str, Any] | None) -> float:
    c = counters or {}
    life = c.get("lifecycle") if isinstance(c.get("lifecycle"), dict) else {}
    return float(life.get("paused_seconds", c.get("paused_seconds", 0)) or 0)


async def _account_paused(s: AsyncSession, run: SourcingRun) -> None:
    """Active-runtime accounting (max_runtime_minutes ignores time spent paused): fold the pause into
    ``counters.lifecycle.paused_seconds``. The counters dict is merged, never replaced, and is kept a complete computed snapshot
    (readers treat a non-empty dict as the computed counters)."""
    if run.paused_at is None:
        return
    paused = max(0.0, (datetime.now(UTC) - run.paused_at).total_seconds())
    c = dict(run.counters or {})
    if "companies" not in c:
        c = {**(await compute_counters(s, run.id)), **c}
        c["progress"] = progress_value(run.target_definition, c)
        c["target"] = run.target_count
    total = round(paused_seconds(c) + paused, 1)
    c.pop("paused_seconds", None)
    c["lifecycle"] = {**(c.get("lifecycle") if isinstance(c.get("lifecycle"), dict) else {}), "paused_seconds": total}
    run.counters = c
    run.paused_at = None


async def stop_run(s: AsyncSession, run_id: int, reason: str = "stopped_by_user") -> SourcingRun:
    """Graceful stop: no new work is handed out, queued jobs are cancelled, in-flight jobs may finish
    and commit; the supervisor flips the run to 'completed' once nothing is leased."""
    run = await _get(s, run_id)
    if run.status in TERMINAL or run.status in ("draft",):
        raise RunStateError(f"cannot stop a run in state {run.status}")
    if run.status == "paused":
        await _account_paused(s, run)
    run.status = "stopping"
    run.stop_reason = run.stop_reason or reason
    await cancel_queued(s, run_id)
    return run


async def cancel_queued(s: AsyncSession, run_id: int) -> int:
    r = await s.execute(text("UPDATE sourcing_jobs SET state='cancelled', finished_at=now() WHERE run_id=:r AND state='queued'"), {"r": run_id})
    return r.rowcount or 0


async def retry_failed(s: AsyncSession, run_id: int) -> int:
    run = await _get(s, run_id)
    n = await jq.retry_jobs(s, run_id=run_id)
    if n and run.status in ("completed", "failed"):
        run.status = "running"
        run.finished_at = None
        run.stop_reason = None
    return n


async def archive_run(s: AsyncSession, run_id: int) -> SourcingRun:
    run = await _get(s, run_id)
    if run.status in ("running", "stopping", "paused"):
        raise RunStateError("stop the run before archiving")
    run.archived = True
    return run


async def duplicate_run(s: AsyncSession, run_id: int, user_id: int | None) -> SourcingRun:
    src = await _get(s, run_id, for_update=False)
    cfg = RunConfig.from_snapshot(src.configuration_snapshot)
    cfg.name = f"{cfg.name} (copy)"[:200]
    return await create_run(s, cfg, user_id=user_id, status="draft")


# ---------------------------------------------------------------- yield counting
PUBLISHED_ORIGINS = ("published", "corroborated")


async def record_yield(s: AsyncSession, run_id: int, email_id: int) -> bool:
    """Count an email toward the run if it satisfies the run's qualification rules.

    Uniqueness is enforced by UNIQUE(run_id, unique_key) so concurrent workers can never double count; the per-company cap
    is serialised per (run, organisation) with a transaction-scoped advisory lock. Returns True when the email was newly counted.

    "Published" always means origin published/corroborated WITH live evidence: an address whose only evidence is archived
    (Common Crawl) or speculative (a permutation guess) is never counted as published.
    """
    from .pipeline.ingest import live_evidence_sql, nonlive_evidence_sql

    run = await _get(s, run_id, for_update=False)
    cfg = RunConfig.from_snapshot(run.configuration_snapshot)
    t = cfg.target
    row = (
        await s.execute(
            text(
                f"""SELECT e.id, e.organisation_id, e.registrable_domain, e.email_normalised, e.origin, e.role_type, e.confidence,
                          e.overall_reachability, e.catch_all_status, e.recipient_evidence_status, e.is_free_provider, e.is_disposable,
                          e.previously_bounced, e.suppressed_at, e.marketing_status, e.syntax_status, e.domain_mail_status, e.contact_id,
                          EXISTS (SELECT 1 FROM contact_evidence ce JOIN evidence_sources es ON es.id = ce.evidence_id
                                  WHERE ce.email_id = e.id AND es.historical) AS has_historical,
                          {live_evidence_sql("e.id")} AS has_live,
                          {nonlive_evidence_sql("e.id")} AS has_nonlive
                   FROM email_addresses e WHERE e.id=:id"""
            ),
            {"id": email_id},
        )
    ).mappings().first()
    if row is None:
        return False
    if row["syntax_status"] == "invalid" or row["is_disposable"] or row["overall_reachability"] == "invalid":
        return False
    if row["has_historical"] and not row["has_live"]:
        return False  # archived (Common Crawl) evidence only: unconfirmed until a live crawl re-observes the address
    origin = row["origin"]
    if origin in PUBLISHED_ORIGINS and row["has_nonlive"] and not row["has_live"]:
        origin = "role_speculative"  # only speculative (permutation) evidence: it is a guess, whatever the stored origin says
    published = origin in PUBLISHED_ORIGINS
    if t.definition.value == "distinct_published_emails" and not published:
        return False  # the definition itself is "published e-mails" (independent of published_only / allow_inferred)
    if t.published_only and not published:
        return False
    if not t.allow_inferred and origin in ("pattern_inferred", "role_speculative"):
        return False
    if t.contact_type == "named" and row["contact_id"] is None:
        return False
    if t.contact_type == "role" and row["role_type"] is None:
        return False
    if t.exclude_free_providers and row["is_free_provider"]:
        return False
    if t.exclude_previous_bounces and row["previously_bounced"]:
        return False
    if t.exclude_suppressed and (row["suppressed_at"] is not None or row["marketing_status"] in ("suppressed", "opted_out")):
        return False
    if t.exclude_catchall_or_inconclusive and (
        row["catch_all_status"] in ("confirmed", "suspected") or row["recipient_evidence_status"] == "inconclusive"
    ):
        return False
    if row["confidence"] < t.min_confidence:
        return False
    if t.definition.value == "campaign_eligible_emails" and row["marketing_status"] != "eligible":
        return False

    if t.uniqueness.value == "organisation":
        key = f"org:{row['organisation_id']}"
    elif t.uniqueness.value == "domain":
        key = f"dom:{row['registrable_domain']}"
    else:
        key = f"email:{row['email_normalised']}"
    if t.definition.value == "distinct_email_domains":
        key = f"dom:{row['registrable_domain']}"
    elif t.definition.value == "business_email_pairs":
        key = f"pair:{row['organisation_id']}:{row['email_normalised']}"

    # per-company cap (only meaningful when several emails per company may count): count + insert under a per (run, org) lock so
    # two workers cannot both see "cap - 1" and both insert
    if t.uniqueness.value == "email" and t.definition.value not in ("distinct_email_domains",):
        await s.execute(text("SELECT pg_advisory_xact_lock(CAST(:r % 2147483647 AS int), CAST(:o % 2147483647 AS int))"),
                        {"r": run_id, "o": row["organisation_id"] or 0})
        n = (await s.execute(text("SELECT count(*) FROM run_yield WHERE run_id=:r AND organisation_id=:o"), {"r": run_id, "o": row["organisation_id"]})).scalar_one()
        if n >= t.max_contacts_per_company:
            return False
    res = await s.execute(
        text(
            """INSERT INTO run_yield (run_id, unique_key, email_id, organisation_id, qualified)
               VALUES (:r, :k, :e, :o, true) ON CONFLICT (run_id, unique_key) DO NOTHING"""
        ),
        {"r": run_id, "k": key, "e": email_id, "o": row["organisation_id"]},
    )
    counted = (res.rowcount or 0) > 0
    if counted and run.target_definition not in ("distinct_companies", "companies_with_domains"):
        # stop at the target from inside the counting transaction: waiting for the next supervisor tick let a single crawl's
        # burst of verify jobs overshoot the target several times over. In-flight jobs still finish (nothing is discarded).
        n = (await s.execute(text("SELECT count(*) FROM run_yield WHERE run_id=:r"), {"r": run_id})).scalar_one()
        if n >= run.target_count:
            flipped = await s.execute(
                text("UPDATE sourcing_runs SET status='stopping', stop_reason='target_reached' WHERE id=:r AND status='running'"), {"r": run_id})
            if flipped.rowcount:
                await cancel_queued(s, run_id)
    return counted


async def verify_leftovers(s: AsyncSession, run_id: int, limit: int = 100_000) -> int:
    """After a run completes, addresses it discovered but never verified (found after the target was reached, or whose verify
    jobs were cancelled by the stop) get a FAST verification under the hidden ``system-verification`` run. They are kept, never
    counted toward the finished run, and exports then show real evidence instead of 'unchecked'."""
    from .system_runs import ensure_system_run

    ids = (await s.execute(text(
        """SELECT DISTINCT e.id FROM contact_evidence ce JOIN email_addresses e ON e.id = ce.email_id
           WHERE ce.run_id = :r AND e.last_validated_at IS NULL LIMIT :n"""), {"r": run_id, "n": limit})).scalars().all()
    if not ids:
        return 0
    sys_run = await ensure_system_run(s, "system-verification")
    for eid in ids:
        await jq.enqueue(s, run_id=sys_run, stage="verify", dedupe_key=f"leftover:{eid}",
                         payload={"email_id": eid, "mode": "fast", "no_yield": True, "after_run": run_id}, priority=80)
    return len(ids)


async def compute_counters(s: AsyncSession, run_id: int) -> dict[str, Any]:
    r = (
        await s.execute(
            text(
                """SELECT
                    (SELECT count(*) FROM run_organisations WHERE run_id=:r) AS companies,
                    (SELECT count(DISTINCT ro.organisation_id) FROM run_organisations ro JOIN organisation_domains d ON d.organisation_id=ro.organisation_id
                       WHERE ro.run_id=:r AND d.verified_match_status IN ('confirmed','high')) AS companies_with_domains,
                    (SELECT count(*) FROM run_yield WHERE run_id=:r) AS qualified,
                    (SELECT count(DISTINCT e.id) FROM contact_evidence ce JOIN email_addresses e ON e.id=ce.email_id WHERE ce.run_id=:r) AS emails_found,
                    (SELECT count(DISTINCT e.registrable_domain) FROM contact_evidence ce JOIN email_addresses e ON e.id=ce.email_id WHERE ce.run_id=:r) AS email_domains,
                    (SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state='queued') AS q_queued,
                    (SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state='leased') AS q_leased,
                    (SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state='done') AS q_done,
                    (SELECT count(*) FROM sourcing_jobs WHERE run_id=:r AND state IN ('failed','dead')) AS q_failed"""
            ),
            {"r": run_id},
        )
    ).mappings().one()
    return dict(r)


def progress_value(definition: str, c: dict[str, Any]) -> int:
    c = c or {}
    return int({
        "distinct_companies": c.get("companies", 0),
        "companies_with_domains": c.get("companies_with_domains", 0),
        "distinct_email_domains": c.get("qualified", 0),
    }.get(definition, c.get("qualified", 0)) or 0)


async def evaluate_run(s: AsyncSession, run_id: int) -> str | None:
    """Refresh counters and apply stop conditions. Returns the new status if it changed."""
    run = await _get(s, run_id)
    if (run.configuration_snapshot or {}).get("system"):
        return None  # hidden system runs (refresh / verification batches / exports) never auto-complete
    c = await compute_counters(s, run_id)
    cfg = RunConfig.from_snapshot(run.configuration_snapshot)
    c["progress"] = progress_value(run.target_definition, c)
    c["target"] = run.target_count
    prev = run.counters or {}
    for k in PRESERVED_COUNTERS:  # written by other stages / lifecycle transitions; not computed here
        if k in prev:
            c[k] = prev[k]
    run.counters = c
    changed = None
    now = datetime.now(UTC)
    if run.status == "running":
        if c["progress"] >= run.target_count:
            run.status, run.stop_reason = "stopping", "target_reached"
            await cancel_queued(s, run_id)
            changed = "stopping"
        elif cfg.target.max_runtime_minutes and run.started_at and active_runtime_s(run, now) > cfg.target.max_runtime_minutes * 60:
            run.status, run.stop_reason = "stopping", "max_runtime"
            await cancel_queued(s, run_id)
            changed = "stopping"
        elif cfg.target.max_work_jobs and (c["q_done"] + c["q_failed"]) >= cfg.target.max_work_jobs:
            run.status, run.stop_reason = "stopping", "work_budget"
            await cancel_queued(s, run_id)
            changed = "stopping"
        elif c["q_queued"] == 0 and c["q_leased"] == 0:
            run.status, run.finished_at = "completed", now
            run.stop_reason = "sources_exhausted" if c["progress"] < run.target_count else "target_reached"
            changed = "completed"
            await verify_leftovers(s, run_id)
    if run.status == "stopping" and c["q_leased"] == 0:
        # children enqueued by jobs that were still in flight when the run stopped are cancelled too (jq.enqueue already refuses
        # new work for a stopping run; this sweeps anything that raced the status change)
        await cancel_queued(s, run_id)
        run.status, run.finished_at = "completed", now
        changed = "completed"
        await verify_leftovers(s, run_id)
    return changed


PRESERVED_COUNTERS = ("gap_yield", "lifecycle", "paused_seconds")  # (top-level paused_seconds: written by an earlier build)


def active_runtime_s(run: SourcingRun, now: datetime | None = None) -> float:
    """Seconds the run has actually been running: wall time since start minus accumulated (and current) paused time."""
    now = now or datetime.now(UTC)
    if run.started_at is None:
        return 0.0
    paused = paused_seconds(run.counters)
    if run.paused_at is not None:
        paused += max(0.0, (now - run.paused_at).total_seconds())
    return max(0.0, (now - run.started_at).total_seconds() - paused)


async def start_due_scheduled(s: AsyncSession) -> list[int]:
    rows = (await s.execute(select(SourcingRun.id).where(SourcingRun.status == "scheduled", SourcingRun.scheduled_for <= datetime.now(UTC)))).scalars().all()
    for rid in rows:
        await start_run(s, rid)
    return list(rows)


async def run_is_active(s: AsyncSession, run_id: int | None) -> bool:
    if run_id is None:
        return True
    st = (await s.execute(select(SourcingRun.status).where(SourcingRun.id == run_id))).scalar_one_or_none()
    return st == "running"


async def active_run_ids(s: AsyncSession, *, include_system: bool = True) -> list[int]:
    q = select(SourcingRun.id).where(SourcingRun.status.in_(("running", "stopping")))
    if not include_system:
        q = q.where(SourcingRun.configuration_snapshot["system"].astext.is_distinct_from("true"))
    return list((await s.execute(q.order_by(SourcingRun.id))).scalars().all())
