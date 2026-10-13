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
    cfg = RunConfig.model_validate(run.configuration_snapshot)
    run.status = "running"
    run.started_at = run.started_at or datetime.now(UTC)
    run.finished_at = None
    run.stop_reason = None
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
    run.paused_at = None
    return run


async def stop_run(s: AsyncSession, run_id: int, reason: str = "stopped_by_user") -> SourcingRun:
    """Graceful stop: no new work is handed out, queued jobs are cancelled, in-flight jobs may finish
    and commit; the supervisor flips the run to 'completed' once nothing is leased."""
    run = await _get(s, run_id)
    if run.status in TERMINAL or run.status in ("draft",):
        raise RunStateError(f"cannot stop a run in state {run.status}")
    run.status = "stopping"
    run.stop_reason = run.stop_reason or reason
    await s.execute(text("UPDATE sourcing_jobs SET state='cancelled', finished_at=now() WHERE run_id=:r AND state IN ('queued')"), {"r": run_id})
    return run


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
    cfg = RunConfig.model_validate(src.configuration_snapshot)
    cfg.name = f"{cfg.name} (copy)"[:200]
    return await create_run(s, cfg, user_id=user_id, status="draft")


# ---------------------------------------------------------------- yield counting
async def record_yield(s: AsyncSession, run_id: int, email_id: int) -> bool:
    """Count an email toward the run if it satisfies the run's qualification rules.

    Uniqueness is enforced by UNIQUE(run_id, unique_key) so concurrent workers can never double count.
    Returns True when the email was newly counted.
    """
    run = await _get(s, run_id, for_update=False)
    cfg = RunConfig.model_validate(run.configuration_snapshot)
    t = cfg.target
    row = (
        await s.execute(
            text(
                """SELECT e.id, e.organisation_id, e.registrable_domain, e.email_normalised, e.origin, e.role_type, e.confidence,
                          e.overall_reachability, e.catch_all_status, e.recipient_evidence_status, e.is_free_provider, e.is_disposable,
                          e.previously_bounced, e.suppressed_at, e.marketing_status, e.syntax_status, e.domain_mail_status, e.contact_id
                   FROM email_addresses e WHERE e.id=:id"""
            ),
            {"id": email_id},
        )
    ).mappings().first()
    if row is None:
        return False
    if row["syntax_status"] == "invalid" or row["is_disposable"] or row["overall_reachability"] == "invalid":
        return False
    if t.published_only and row["origin"] not in ("published", "corroborated"):
        return False
    if not t.allow_inferred and row["origin"] in ("pattern_inferred", "role_speculative"):
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

    # per-company cap (only meaningful when several emails per company may count)
    if t.uniqueness.value == "email" and t.definition.value not in ("distinct_email_domains",):
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
    return (res.rowcount or 0) > 0


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
    return {
        "distinct_companies": c["companies"],
        "companies_with_domains": c["companies_with_domains"],
        "distinct_email_domains": c["qualified"],
    }.get(definition, c["qualified"])


async def evaluate_run(s: AsyncSession, run_id: int) -> str | None:
    """Refresh counters and apply stop conditions. Returns the new status if it changed."""
    run = await _get(s, run_id)
    if (run.configuration_snapshot or {}).get("system"):
        return None  # hidden system runs (refresh / verification batches / exports) never auto-complete
    c = await compute_counters(s, run_id)
    cfg = RunConfig.model_validate(run.configuration_snapshot)
    c["progress"] = progress_value(run.target_definition, c)
    c["target"] = run.target_count
    run.counters = c
    changed = None
    now = datetime.now(UTC)
    if run.status == "running":
        if c["progress"] >= run.target_count:
            run.status, run.stop_reason = "stopping", "target_reached"
            await s.execute(text("UPDATE sourcing_jobs SET state='cancelled', finished_at=now() WHERE run_id=:r AND state='queued'"), {"r": run_id})
            changed = "stopping"
        elif cfg.target.max_runtime_minutes and run.started_at and (now - run.started_at).total_seconds() > cfg.target.max_runtime_minutes * 60:
            run.status, run.stop_reason = "stopping", "max_runtime"
            await s.execute(text("UPDATE sourcing_jobs SET state='cancelled', finished_at=now() WHERE run_id=:r AND state='queued'"), {"r": run_id})
            changed = "stopping"
        elif cfg.target.max_work_jobs and (c["q_done"] + c["q_failed"]) >= cfg.target.max_work_jobs:
            run.status, run.stop_reason = "stopping", "work_budget"
            await s.execute(text("UPDATE sourcing_jobs SET state='cancelled', finished_at=now() WHERE run_id=:r AND state='queued'"), {"r": run_id})
            changed = "stopping"
        elif c["q_queued"] == 0 and c["q_leased"] == 0:
            run.status, run.finished_at = "completed", now
            run.stop_reason = "sources_exhausted" if c["progress"] < run.target_count else "target_reached"
            changed = "completed"
    if run.status == "stopping" and c["q_leased"] == 0:
        run.status, run.finished_at = "completed", now
        changed = "completed"
    return changed


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


async def active_run_ids(s: AsyncSession) -> list[int]:
    return list((await s.execute(select(SourcingRun.id).where(SourcingRun.status.in_(("running", "stopping"))))).scalars().all())
