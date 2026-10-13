"""Run configuration validation + infrastructure estimate (used by /api/runs/validate and /estimate)."""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pycountry
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import Category, SourceAdapterState
from ..run_config import RunConfig
from ..sources.access_policy import config_problems
from .registry import adapter_catalog

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class FieldError(BaseModel):
    field: str
    message: str


class ValidateOut(BaseModel):
    valid: bool
    errors: list[FieldError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Shortage(BaseModel):
    resource: str
    required: float
    available: float
    message: str


class Restriction(BaseModel):
    code: str
    severity: str  # info | warning | blocking
    message: str


class RunEstimateOut(BaseModel):
    valid: bool
    errors: list[FieldError] = Field(default_factory=list)
    infra: dict[str, Any] = Field(default_factory=dict)
    history: dict[str, Any] = Field(default_factory=dict)
    estimated_businesses_needed: int | None = None
    estimated_requests: int | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    shortages: list[Shortage] = Field(default_factory=list)
    restrictions: list[Restriction] = Field(default_factory=list)


def uploads_dir(settings: Settings) -> Path:
    return (Path(settings.data_dir) / "uploads").resolve()


def _pydantic_errors(e: ValidationError) -> list[FieldError]:
    return [FieldError(field=".".join(str(p) for p in err["loc"]), message=str(err["msg"]).removeprefix("Value error, ")) for err in e.errors()]


async def validate_config(session: AsyncSession, raw: dict[str, Any], settings: Settings) -> tuple[RunConfig | None, ValidateOut]:
    """Field-level validation of a wizard payload (schema + referential + safety checks)."""
    try:
        cfg = RunConfig.model_validate(raw)
    except ValidationError as e:
        return None, ValidateOut(valid=False, errors=_pydantic_errors(e))
    errors: list[FieldError] = []
    warnings: list[str] = []
    for i, c in enumerate(cfg.countries):
        if len(c) != 2 or pycountry.countries.get(alpha_2=c) is None:
            errors.append(FieldError(field=f"countries.{i}", message=f"'{c}' is not a valid ISO 3166-1 alpha-2 country code."))
    for i, d in enumerate(cfg.imported_domains):
        if not _HOST.match(d.strip().lower()):
            errors.append(FieldError(field=f"imported_domains.{i}", message=f"'{d[:80]}' is not a valid domain name (use e.g. example.com without http://)."))
    wanted = set(cfg.category_ids) | ({cfg.destination_category_id} if cfg.destination_category_id else set())
    if wanted:
        have = set((await session.execute(select(Category.id).where(Category.id.in_(wanted)))).scalars())
        for cid in sorted(wanted - have):
            errors.append(FieldError(field="category_ids", message=f"Category {cid} does not exist."))
    if cfg.schedule_at:
        try:
            dt = datetime.fromisoformat(cfg.schedule_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt <= datetime.now(UTC):
                errors.append(FieldError(field="schedule_at", message="The scheduled time must be in the future."))
        except ValueError:
            errors.append(FieldError(field="schedule_at", message="schedule_at must be an ISO 8601 timestamp, e.g. 2030-01-31T09:00:00Z."))
    for adapter_id, params in cfg.source_params.items():
        for where, msg in config_problems(params, settings, prefix=f"source_params.{adapter_id}."):
            errors.append(FieldError(field=where, message=msg))
    catalog = adapter_catalog()
    states = {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}
    for i, sid in enumerate(cfg.sources):
        if catalog is not None and sid not in catalog:
            errors.append(FieldError(field=f"sources.{i}", message=f"Unknown source '{sid}'."))
            continue
        st = states.get(sid)
        if st is not None and not st.enabled:
            errors.append(FieldError(field=f"sources.{i}", message=f"Source '{sid}' is disabled. Enable it on the Sources page or remove it."))
        info = (catalog or {}).get(sid)
        if info:
            if info.get("info_only") or not info.get("automated_collection_permitted", True):
                errors.append(FieldError(field=f"sources.{i}", message=f"Source '{sid}' does not permit automated collection and cannot be used in a run."))
            elif info.get("requires_config") and not (st and st.config) and not cfg.source_params.get(sid):
                warnings.append(f"Source '{sid}' needs configuration (API key, path or URL) that has not been supplied.")
            cov = info.get("country_coverage") or []
            if cov and "*" not in cov and not set(cfg.countries) & set(cov):
                warnings.append(f"Source '{sid}' does not cover the selected countries ({', '.join(cfg.countries)}).")
    if cfg.verification.mode == "custom" and not isinstance(cfg.verification.custom, dict):
        errors.append(FieldError(field="verification.custom", message="Custom verification options must be an object."))
    if cfg.performance.preset != "advanced" and (cfg.performance.http_concurrency or 0) > 200:
        warnings.append("High HTTP concurrency selected without the 'advanced' performance preset.")
    return cfg, ValidateOut(valid=not errors, errors=errors, warnings=warnings)


PRESET_CONCURRENCY = {"conservative": 16, "balanced": 48, "advanced": 128}
RAM_BASE_PER_WORKER_MB = 300
RAM_PER_CONCURRENT_FETCH_MB = 3
BROWSER_WORKER_MB = 400
BYTES_PER_RESULT_ROW = 2048


async def estimate_config(session: AsyncSession, raw: dict[str, Any], settings: Settings) -> RunEstimateOut:
    cfg, v = await validate_config(session, raw, settings)
    if cfg is None or not v.valid:
        return RunEstimateOut(valid=False, errors=v.errors, warnings=v.warnings)
    perf = cfg.performance
    http_conc = perf.http_concurrency or PRESET_CONCURRENCY[perf.preset]
    procs_needed = math.ceil(http_conc / max(1, settings.worker_concurrency))
    if perf.worker_limit:
        procs_needed = min(procs_needed, perf.worker_limit)
    ram_needed = procs_needed * RAM_BASE_PER_WORKER_MB + http_conc * RAM_PER_CONCURRENT_FETCH_MB + perf.browser_workers * BROWSER_WORKER_MB
    vm = psutil.virtual_memory()
    try:
        free_disk = psutil.disk_usage(settings.data_dir if Path(settings.data_dir).exists() else "/").free
    except OSError:
        free_disk = 0
    alive = (await session.execute(text("SELECT count(*) FROM worker_heartbeats WHERE last_seen_at > now() - make_interval(secs => :t)"), {"t": settings.worker_heartbeat_seconds * 4})).scalar_one()

    # historical yield: published/corroborated emails per organisation that has a confirmed domain
    row = (await session.execute(text(
        """SELECT (SELECT count(*) FROM email_addresses WHERE origin IN ('published','corroborated')) AS emails,
                  (SELECT count(DISTINCT organisation_id) FROM organisation_domains WHERE verified_match_status IN ('confirmed','high')) AS orgs"""))).one()
    emails, orgs = int(row[0]), int(row[1])
    yield_per_org = (emails / orgs) if orgs >= 20 and emails > 0 else None
    businesses_needed = None
    target = cfg.target
    if target.definition.value in ("distinct_companies", "companies_with_domains"):
        businesses_needed = target.count
    elif yield_per_org:
        businesses_needed = math.ceil(target.count / yield_per_org)
    est_requests = businesses_needed * cfg.crawl_max_pages if businesses_needed else None
    est_rows = target.count
    est_disk_mb = round(est_rows * BYTES_PER_RESULT_ROW / 1e6, 1)

    shortages: list[Shortage] = []
    if alive < procs_needed:
        shortages.append(Shortage(resource="worker_processes", required=procs_needed, available=alive,
                                  message=f"This configuration can use {procs_needed} worker process(es) but {alive} are running; it will run slower than requested."))
    if ram_needed > vm.available / 1e6:
        shortages.append(Shortage(resource="ram_mb", required=ram_needed, available=round(vm.available / 1e6), message="Estimated memory need exceeds the memory currently available on this host."))
    if est_disk_mb * 1e6 > free_disk:
        shortages.append(Shortage(resource="disk_mb", required=est_disk_mb, available=round(free_disk / 1e6), message="Estimated storage need exceeds free disk space."))
    if perf.max_ram_mb and ram_needed > perf.max_ram_mb:
        shortages.append(Shortage(resource="configured_ram_limit_mb", required=ram_needed, available=perf.max_ram_mb, message="The RAM ceiling you configured is lower than the estimate; concurrency will be throttled."))

    restrictions: list[Restriction] = []
    warnings = list(v.warnings)
    catalog = adapter_catalog()
    states = {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}
    for sid in cfg.sources:
        info = (catalog or {}).get(sid)
        if info and info.get("requires_config") and not ((states.get(sid) and states[sid].config) or cfg.source_params.get(sid)):
            restrictions.append(Restriction(code="source_unconfigured", severity="blocking", message=f"Source '{sid}' is not configured, so it will produce no records."))
    if catalog is None:
        warnings.append("The source registry could not be loaded, so source availability could not be checked.")
    if cfg.verification.mode in ("deep", "custom") and not settings.smtp_diagnostics_enabled:
        restrictions.append(Restriction(code="smtp_not_enabled", severity="warning", message="SMTP recipient diagnostics are not enabled on this deployment; deep verification will run DNS/MX and catch-all-free checks only."))
    if perf.browser_workers:
        import importlib.util

        if importlib.util.find_spec("playwright") is None:
            restrictions.append(Restriction(code="browser_unavailable", severity="warning", message="Browser workers were requested but Playwright is not installed; JavaScript-rendered sites will be skipped."))
    if not settings.contact_email:
        warnings.append("LEAD_CONTACT_EMAIL is not set; the crawler User-Agent has no contact address (some sites block such bots).")
    if yield_per_org is None and target.definition.value not in ("distinct_companies", "companies_with_domains"):
        warnings.append("Not enough historical data to estimate how many businesses are needed for this target.")
    assumptions = [
        f"Per worker process: {RAM_BASE_PER_WORKER_MB} MB base + {RAM_PER_CONCURRENT_FETCH_MB} MB per concurrent fetch + {BROWSER_WORKER_MB} MB per browser worker.",
        f"Each worker process runs up to {settings.worker_concurrency} jobs at once (LEAD_WORKER_CONCURRENCY).",
        f"Result storage is approximated at {BYTES_PER_RESULT_ROW} bytes per qualified row.",
        "No duration is estimated before a run starts; run ETAs are computed from observed throughput once the run is executing.",
    ]
    return RunEstimateOut(
        valid=True,
        infra={
            "effective_http_concurrency": http_conc, "worker_processes_needed": procs_needed, "worker_processes_online": int(alive),
            "estimated_ram_mb": ram_needed, "available_ram_mb": round(vm.available / 1e6), "estimated_disk_mb": est_disk_mb,
            "free_disk_mb": round(free_disk / 1e6), "browser_workers": perf.browser_workers, "batch_size": perf.batch_size,
            "cpu_count": psutil.cpu_count(),
        },
        history={"published_emails": emails, "organisations_with_domain": orgs, "emails_per_organisation": round(yield_per_org, 3) if yield_per_org else None},
        estimated_businesses_needed=businesses_needed,
        estimated_requests=est_requests,
        assumptions=assumptions, warnings=warnings, shortages=shortages, restrictions=restrictions,
    )

