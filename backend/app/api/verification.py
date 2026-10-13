"""Verification centre: overview, tabs, batches, estimate, delivery-event import, SMTP diagnostics and permissions."""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import logging
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, String, cast, func, literal, literal_column, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import runs as core
from ..db import get_session
from ..models import DomainPermission, EmailAddress, SourcingJob, User, ValidationResult, VerificationBatch
from ..security import ApiError, app_settings, audit, require_role
from ..settings_schema import load_section
from ..settings_store import get_setting
from ..system_runs import new_system_run
from .common import Ok, Page, SortSpec, apply_keyset, capped_count, finish_page
from .contacts import ContactRow, _base_from, contact_rows, mask_pii_list
from .dashboard import _from, _sample_pct, _scaled
from .filters import ContactFilterSpec, build_conditions, contact_filters
from .registry import require
from .uploads import read_limited, sniff

log = logging.getLogger("api.verification")
router = APIRouter(prefix="/api/verification", tags=["verification"], dependencies=[Depends(require_role("viewer"))])
E = EmailAddress
MAX_BATCH = 500_000
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
TABS = ("invalid", "inconclusive", "catch_all", "needs_recheck")


class Bucket(BaseModel):
    key: str
    count: int


class VerificationOverview(BaseModel):
    approximate: bool
    total_emails: int
    never_validated: int
    untested_recipient: int
    invalid: int
    inconclusive: int
    catch_all: int
    needs_recheck: int
    reachability: list[Bucket]
    verify_queue: dict[str, int]
    active_batches: int
    validity_days: int
    smtp_enabled: bool


class BatchOut(BaseModel):
    id: int
    name: str
    mode: str
    status: str
    total: int
    done: int
    failed: int
    queued: int
    percent: float
    created_at: datetime
    created_by: int | None
    run_id: int | None
    settings: dict[str, Any]


class BatchCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["fast", "deep", "custom"] = "fast"
    options: dict[str, Any] = Field(default_factory=dict, description="VerifyOptions overrides for mode=custom")
    email_ids: list[int] = Field(default_factory=list, max_length=100000)
    filters: ContactFilterSpec | None = None
    confirm_all: bool = Field(False, description="Required to verify every address when no filters/ids are given")


class BatchUploadOut(BaseModel):
    batch: BatchOut
    rows_read: int
    matched: int
    unmatched: int
    unmatched_sample: list[str]


class EstimateIn(BaseModel):
    mode: Literal["fast", "deep", "custom"] = "fast"
    options: dict[str, Any] = Field(default_factory=dict)
    filters: ContactFilterSpec | None = None
    email_ids: list[int] = Field(default_factory=list, max_length=100000)
    count: int | None = Field(None, ge=1, le=100_000_000)


class EstimateOut(BaseModel):
    mode: str
    addresses: int | None
    addresses_capped: bool = False
    cost: dict[str, Any]
    warnings: list[str]


class HistoryRow(BaseModel):
    id: int
    email_id: int
    check_type: str
    result: str
    reason_code: str | None
    provider: str | None
    observed_at: datetime
    expires_at: datetime | None
    verifier_version: str


class DeliveryImportOut(BaseModel):
    received: int
    inserted: int
    duplicates: int
    unknown_email: int
    invalid: int
    suppressions_added: int
    emails_affected: int
    errors: list[str]


class SmtpStatus(BaseModel):
    enabled: bool
    helo_host: str | None
    mail_from_domain: str | None
    configured_authorised_domains: list[str]
    permission_rows: int
    egress: dict[str, Any] | None
    ready: bool
    problems: list[str]


class PermissionOut(BaseModel):
    domain: str
    smtp_probe_allowed: bool
    basis: str
    granted_by: int | None
    granted_at: datetime


class PermissionIn(BaseModel):
    smtp_probe_allowed: bool = True
    basis: str = Field(min_length=10, max_length=300, description="Why probing this domain is authorised (owned domain, written consent...)")


def _norm_domain(d: str) -> str:
    v = d.strip().lower().rstrip(".")
    if not HOST_RE.match(v):
        raise ApiError(422, "invalid_domain", f"'{d[:80]}' is not a valid domain name.")
    return v


# --------------------------------------------------------------------------- overview & tabs
@router.get("/overview", response_model=VerificationOverview)
async def verification_overview(request: Request, session: AsyncSession = Depends(get_session)) -> VerificationOverview:
    validity = (await load_section(session, "validation")).validity_days
    pct = await _sample_pct(session, "email_addresses")
    r = (await session.execute(text(
        f"""SELECT count(*), count(*) FILTER (WHERE last_validated_at IS NULL), count(*) FILTER (WHERE recipient_evidence_status='untested'),
                   count(*) FILTER (WHERE overall_reachability='invalid'), count(*) FILTER (WHERE recipient_evidence_status='inconclusive'),
                   count(*) FILTER (WHERE catch_all_status IN ('confirmed','suspected')),
                   count(*) FILTER (WHERE last_validated_at IS NULL OR last_validated_at < now() - make_interval(days => :d))
            FROM {_from('email_addresses', pct)}"""), {"d": validity})).one()
    reach = [Bucket(key=k or "unknown", count=_scaled(n, pct)) for k, n in (await session.execute(text(f"SELECT overall_reachability, count(*) FROM {_from('email_addresses', pct)} GROUP BY 1 ORDER BY 2 DESC"))).all()]
    q = {s: n for s, n in (await session.execute(text("SELECT state, count(*) FROM sourcing_jobs WHERE stage='verify' AND state IN ('queued','leased','failed','dead','paused') GROUP BY 1"))).all()}
    active = (await session.execute(select(func.count()).select_from(VerificationBatch).where(VerificationBatch.status.in_(("queued", "running", "paused"))))).scalar_one()
    s = [_scaled(int(x), pct) for x in r]
    return VerificationOverview(
        approximate=pct < 100, total_emails=s[0], never_validated=s[1], untested_recipient=s[2], invalid=s[3], inconclusive=s[4], catch_all=s[5], needs_recheck=s[6],
        reachability=reach, verify_queue=q, active_batches=active, validity_days=validity, smtp_enabled=app_settings(request).smtp_diagnostics_enabled,
    )


@router.get("/emails", response_model=Page[ContactRow])
async def verification_tab(
    tab: Literal["invalid", "inconclusive", "catch_all", "needs_recheck"], f: ContactFilterSpec = Depends(contact_filters), sort: str = "id", direction: str = "desc",
    limit: int = Query(50, ge=1, le=500), cursor: str | None = None, session: AsyncSession = Depends(get_session),
) -> Page[ContactRow]:
    conds = await build_conditions(session, f)
    validity = (await load_section(session, "validation")).validity_days
    conds.append({
        "invalid": E.overall_reachability == "invalid",
        "inconclusive": E.recipient_evidence_status == "inconclusive",
        "catch_all": E.catch_all_status.in_(("confirmed", "suspected")),
        "needs_recheck": (E.last_validated_at.is_(None)) | (E.last_validated_at < func.now() - func.make_interval(0, 0, 0, validity)),
    }[tab])
    return await contact_rows(session, conds, sort, direction, limit, cursor)


@router.get("/history", response_model=Page[HistoryRow])
async def verification_history(
    email_id: int | None = None, check_type: str | None = Query(None, max_length=32), result: str | None = Query(None, max_length=32),
    limit: int = Query(50, ge=1, le=500), cursor: str | None = None, session: AsyncSession = Depends(get_session),
) -> Page[HistoryRow]:
    V = ValidationResult
    sorts = {"id": SortSpec(V.id, "int")}
    stmt = select(V, V.id.label("_sv"), V.id.label("_id"))
    if email_id is not None:
        stmt = stmt.where(V.email_id == email_id)
    if check_type:
        stmt = stmt.where(V.check_type == check_type)
    if result:
        stmt = stmt.where(V.result == result)
    stmt = apply_keyset(stmt, sorts, "id", "desc", V.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "desc", limit)
    return Page[HistoryRow](items=[HistoryRow(id=v.id, email_id=v.email_id, check_type=v.check_type, result=v.result, reason_code=v.reason_code, provider=v.provider,
                                              observed_at=v.observed_at, expires_at=v.expires_at, verifier_version=v.verifier_version) for v, *_ in rows], next_cursor=nxt)


# --------------------------------------------------------------------------- estimate
@router.post("/estimate", response_model=EstimateOut, dependencies=[Depends(require_role("operator"))])
async def estimate_verification(body: EstimateIn, request: Request, session: AsyncSession = Depends(get_session)) -> EstimateOut:
    pipe = require("app.verify.pipeline", "Verification cost estimation")
    n, capped = body.count, False
    if n is None:
        conds = await build_conditions(session, body.filters or ContactFilterSpec(email_ids=body.email_ids))
        n, capped = await capped_count(session, select(literal_column("1")).select_from(_base_from()).where(*conds), cap=1_000_000, timeout_ms=3000)
    try:
        opts = pipe.VerifyOptions.for_mode(body.mode)
        if body.mode == "custom":
            valid = {f.name for f in dataclasses.fields(pipe.VerifyOptions)}
            bad = sorted(set(body.options) - valid)
            if bad:
                raise ApiError(422, "invalid_options", f"Unknown verification option(s): {', '.join(bad)}.")
            opts = dataclasses.replace(opts, **body.options)
        cost = pipe.estimate_cost(opts, count=n or 1)
    except (TypeError, ValueError) as e:
        raise ApiError(422, "invalid_options", f"Invalid verification options: {e}") from None
    warnings = []
    st = app_settings(request)
    if (opts.smtp or opts.catch_all) and not st.smtp_diagnostics_enabled:
        warnings.append("SMTP diagnostics are not enabled on this deployment; SMTP and catch-all stages will be skipped.")
    return EstimateOut(mode=body.mode, addresses=n, addresses_capped=capped, cost=cost, warnings=warnings)


# --------------------------------------------------------------------------- batches
async def _batch_out(session: AsyncSession, b: VerificationBatch, refresh: bool = True) -> BatchOut:
    queued = failed = done = 0
    if b.run_id:
        for st, n in (await session.execute(select(SourcingJob.state, func.count()).where(SourcingJob.run_id == b.run_id).group_by(SourcingJob.state))).all():
            if st == "done":
                done += n
            elif st in ("failed", "dead"):
                failed += n
            elif st in ("queued", "leased", "paused"):
                queued += n
    finished = done + failed
    if refresh and b.status in ("queued", "running"):
        new = b.status
        if queued == 0 and b.total > 0:
            new = "done"
        elif finished > 0 or b.status == "running":
            new = "running"
        if new != b.status or b.done != finished:
            b.status, b.done = new, finished
            if new == "done" and b.run_id:
                await session.execute(text("UPDATE sourcing_runs SET status='completed', finished_at=now() WHERE id=:r AND status='running'"), {"r": b.run_id})
            await session.commit()
    return BatchOut(id=b.id, name=b.name, mode=b.mode, status=b.status, total=b.total, done=finished, failed=failed, queued=queued,
                    percent=round(100.0 * finished / b.total, 1) if b.total else 0.0, created_at=b.created_at, created_by=b.created_by, run_id=b.run_id, settings=b.settings or {})


async def _create_batch(session: AsyncSession, user: User, body: BatchCreateIn, email_conds: list | None, ids: list[int] | None) -> VerificationBatch:
    opts_json: dict[str, Any] = {}
    if body.mode == "custom":
        pipe = require("app.verify.pipeline", "Custom verification")
        valid = {f.name for f in dataclasses.fields(pipe.VerifyOptions)}
        bad = sorted(set(body.options) - valid)
        if bad:
            raise ApiError(422, "invalid_options", f"Unknown verification option(s): {', '.join(bad)}.")
        opts_json = body.options
    batch = VerificationBatch(name=body.name.strip(), mode=body.mode, status="queued", created_by=user.id, settings={"options": opts_json})
    session.add(batch)
    await session.flush()
    run_id = await new_system_run(session, f"system-verify-batch-{batch.id}")
    batch.run_id = run_id
    sel_where = email_conds if email_conds is not None else [E.id.in_(ids or [])]
    payload = func.jsonb_build_object("email_id", E.id, "mode", cast(literal(body.mode), String), "batch_id", cast(literal(batch.id), BigInteger), "options", cast(literal(json.dumps(opts_json)), JSONB))
    sel = (
        select(literal(run_id), literal("verify"), literal(f"b{batch.id}:") + cast(E.id, String), payload)
        .select_from(_base_from()).where(*sel_where).order_by(E.id).limit(MAX_BATCH)
    )
    stmt = pg_insert(SourcingJob).from_select(["run_id", "stage", "dedupe_key", "payload"], sel).on_conflict_do_nothing()
    res = await session.execute(stmt)
    batch.total = res.rowcount or 0
    if batch.total == 0:
        raise ApiError(422, "empty_batch", "No addresses matched, so there is nothing to verify.")
    return batch


@router.get("/batches", response_model=Page[BatchOut])
async def list_batches(status: str | None = Query(None, max_length=12), limit: int = Query(50, ge=1, le=200), cursor: str | None = None, session: AsyncSession = Depends(get_session)) -> Page[BatchOut]:
    sorts = {"id": SortSpec(VerificationBatch.id, "int")}
    stmt = select(VerificationBatch, VerificationBatch.id.label("_sv"), VerificationBatch.id.label("_id"))
    if status:
        stmt = stmt.where(VerificationBatch.status == status)
    stmt = apply_keyset(stmt, sorts, "id", "desc", VerificationBatch.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "desc", limit)
    return Page[BatchOut](items=[await _batch_out(session, r[0]) for r in rows], next_cursor=nxt)


@router.post("/batches", response_model=BatchOut, status_code=201)
async def create_batch(body: BatchCreateIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> BatchOut:
    if body.email_ids:
        batch = await _create_batch(session, user, body, None, sorted(set(body.email_ids)))
    else:
        f = body.filters or ContactFilterSpec()
        if f.is_empty() and not body.confirm_all:
            raise ApiError(422, "scope_required", "Choose email_ids or filters, or set confirm_all=true to verify every address.")
        batch = await _create_batch(session, user, body, await build_conditions(session, f), None)
    await audit(session, user, "verification.batch_create", "batch", batch.id, {"mode": body.mode, "total": batch.total, "name": batch.name})
    await session.commit()
    return await _batch_out(session, batch)


@router.post("/batches/upload", response_model=BatchUploadOut, status_code=201)
async def create_batch_from_csv(
    request: Request, file: UploadFile = File(...), name: str = Query(..., min_length=1, max_length=200), mode: Literal["fast", "deep"] = "fast",
    user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session),
) -> BatchUploadOut:
    """CSV with one e-mail per row (an 'email' column, or the first column). Only addresses already in the database can be verified."""
    data = await read_limited(file, app_settings(request).max_upload_mb * 1024 * 1024)
    if sniff(data) != "text":
        raise ApiError(422, "unsupported_file", "Upload a CSV text file.")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    if not rows:
        raise ApiError(422, "empty_file", "The file has no rows.")
    col = 0
    header = [c.strip().lower() for c in rows[0]]
    if "email" in header or "email address" in header or "email_address" in header:
        col = header.index("email" if "email" in header else ("email address" if "email address" in header else "email_address"))
        rows = rows[1:]
    elif "@" not in rows[0][0]:
        rows = rows[1:]
    if len(rows) > 200_000:
        raise ApiError(413, "too_many_rows", "At most 200,000 addresses can be uploaded in one file.")
    wanted = list(dict.fromkeys(r[col].strip().lower() for r in rows if len(r) > col and "@" in r[col]))
    found: dict[str, int] = {}
    for i in range(0, len(wanted), 5000):
        chunk = wanted[i:i + 5000]
        for eid, em in (await session.execute(select(E.id, E.email_normalised).where(E.email_normalised.in_(chunk)))).all():
            found[em] = eid
    unmatched = [w for w in wanted if w not in found]
    if not found:
        raise ApiError(422, "empty_batch", "None of the uploaded addresses exist in the database, so there is nothing to verify.")
    body = BatchCreateIn(name=name, mode=mode)
    batch = await _create_batch(session, user, body, None, sorted(found.values()))
    await audit(session, user, "verification.batch_upload", "batch", batch.id, {"mode": mode, "rows": len(wanted), "matched": len(found)})
    await session.commit()
    return BatchUploadOut(batch=await _batch_out(session, batch), rows_read=len(rows), matched=len(found), unmatched=len(unmatched), unmatched_sample=mask_pii_list(unmatched[:10]))


async def _get_batch(session: AsyncSession, batch_id: int) -> VerificationBatch:
    b = await session.get(VerificationBatch, batch_id)
    if b is None:
        raise ApiError(404, "not_found", "Verification batch not found.")
    return b


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def get_batch(batch_id: int, session: AsyncSession = Depends(get_session)) -> BatchOut:
    return await _batch_out(session, await _get_batch(session, batch_id))


@router.post("/batches/{batch_id}/pause", response_model=BatchOut)
async def pause_batch(batch_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> BatchOut:
    b = await _get_batch(session, batch_id)
    if b.status not in ("queued", "running"):
        raise ApiError(409, "invalid_batch_state", f"Only a queued or running batch can be paused; this one is {b.status}.")
    try:
        await core.pause_run(session, b.run_id)
    except core.RunStateError:
        pass
    b.status = "paused"
    await audit(session, user, "verification.batch_pause", "batch", b.id)
    await session.commit()
    return await _batch_out(session, b, refresh=False)


@router.post("/batches/{batch_id}/resume", response_model=BatchOut)
async def resume_batch(batch_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> BatchOut:
    b = await _get_batch(session, batch_id)
    if b.status != "paused":
        raise ApiError(409, "invalid_batch_state", f"Only a paused batch can be resumed; this one is {b.status}.")
    try:
        await core.resume_run(session, b.run_id)
    except core.RunStateError as e:
        raise ApiError(409, "invalid_batch_state", str(e)) from None
    b.status = "running"
    await audit(session, user, "verification.batch_resume", "batch", b.id)
    await session.commit()
    return await _batch_out(session, b, refresh=False)


@router.post("/batches/{batch_id}/cancel", response_model=BatchOut)
async def cancel_batch(batch_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> BatchOut:
    b = await _get_batch(session, batch_id)
    if b.status in ("done", "cancelled"):
        raise ApiError(409, "invalid_batch_state", f"This batch is already {b.status}.")
    await session.execute(text("UPDATE sourcing_jobs SET state='cancelled', finished_at=now() WHERE run_id=:r AND state IN ('queued','paused')"), {"r": b.run_id})
    await session.execute(text("UPDATE sourcing_runs SET status='cancelled', finished_at=now(), stop_reason='batch_cancelled' WHERE id=:r"), {"r": b.run_id})
    b.status = "cancelled"
    await audit(session, user, "verification.batch_cancel", "batch", b.id)
    await session.commit()
    return await _batch_out(session, b, refresh=False)


# --------------------------------------------------------------------------- delivery events
@router.post("/delivery-events", response_model=DeliveryImportOut)
async def import_delivery(request: Request, file: UploadFile = File(...), user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> DeliveryImportOut:
    """Import sending-platform feedback (CSV email,event_type,campaign,occurred_at,detail or a JSON list of such objects)."""
    mod = require("app.verify.delivery", "Delivery-event import")
    data = await read_limited(file, app_settings(request).max_upload_mb * 1024 * 1024)
    if sniff(data) != "text":
        raise ApiError(422, "unsupported_file", "Upload a CSV or JSON file.")
    txt = data.decode("utf-8-sig", errors="replace")
    try:
        if txt.lstrip().startswith(("[", "{")):
            parsed = json.loads(txt)
            rows = parsed.get("events", parsed.get("rows")) if isinstance(parsed, dict) else parsed
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("expected a JSON list of event objects")
        else:
            rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in csv.DictReader(io.StringIO(txt))]
    except (ValueError, json.JSONDecodeError, csv.Error) as e:
        raise ApiError(422, "invalid_file", f"Could not parse the file: {str(e)[:200]}") from None
    if len(rows) > 500_000:
        raise ApiError(413, "too_many_rows", "At most 500,000 events can be imported at once.")
    summary = await mod.import_delivery_events(session, rows)
    d = dataclasses.asdict(summary)
    await audit(session, user, "verification.delivery_import", "delivery_events", None, {k: d[k] for k in ("received", "inserted", "duplicates", "unknown_email", "invalid", "suppressions_added")})
    await session.commit()
    return DeliveryImportOut(received=d["received"], inserted=d["inserted"], duplicates=d["duplicates"], unknown_email=d["unknown_email"], invalid=d["invalid"],
                             suppressions_added=d["suppressions_added"], emails_affected=len(d["affected_email_ids"]), errors=mask_pii_list(d["errors"][:50]))


# --------------------------------------------------------------------------- SMTP diagnostics
@router.get("/smtp", response_model=SmtpStatus)
async def smtp_status(request: Request, session: AsyncSession = Depends(get_session)) -> SmtpStatus:
    st = app_settings(request)
    problems = []
    if not st.smtp_diagnostics_enabled:
        problems.append("SMTP diagnostics are disabled (LEAD_SMTP_DIAGNOSTICS_ENABLED=false); only DNS/MX evidence is collected.")
    if st.smtp_diagnostics_enabled and not st.smtp_helo_host:
        problems.append("LEAD_SMTP_HELO_HOST is not set.")
    if st.smtp_diagnostics_enabled and not st.smtp_mail_from:
        problems.append("LEAD_SMTP_MAIL_FROM is not set (use an address on a domain you own).")
    n = (await session.execute(select(func.count()).select_from(DomainPermission).where(DomainPermission.smtp_probe_allowed.is_(True)))).scalar_one()
    domains = [d.strip().lower() for d in st.smtp_authorised_domains.split(",") if d.strip()]
    if st.smtp_diagnostics_enabled and not domains and not n:
        problems.append("No domain is authorised for SMTP probing; add a domain permission or LEAD_SMTP_AUTHORISED_DOMAINS.")
    egress = await get_setting(session, "smtp_egress_last", None)
    if egress and egress.get("status") == "blocked":
        problems.append("Outbound port 25 appears to be blocked from this host; SMTP results will be inconclusive.")
    return SmtpStatus(
        enabled=st.smtp_diagnostics_enabled, helo_host=st.smtp_helo_host or None, mail_from_domain=st.smtp_mail_from.split("@")[-1] if "@" in st.smtp_mail_from else None,
        configured_authorised_domains=domains, permission_rows=n, egress=egress, ready=st.smtp_diagnostics_enabled and not [p for p in problems if "blocked" not in p and "disabled" not in p], problems=problems,
    )


@router.get("/domain-permissions", response_model=list[PermissionOut])
async def list_domain_permissions(session: AsyncSession = Depends(get_session)) -> list[PermissionOut]:
    rows = (await session.execute(select(DomainPermission).order_by(DomainPermission.domain).limit(1000))).scalars()
    return [PermissionOut(domain=r.domain, smtp_probe_allowed=r.smtp_probe_allowed, basis=r.basis, granted_by=r.granted_by, granted_at=r.granted_at) for r in rows]


@router.put("/domain-permissions/{domain}", response_model=PermissionOut)
async def upsert_domain_permission(domain: str, body: PermissionIn, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> PermissionOut:
    d = _norm_domain(domain)
    row = await session.get(DomainPermission, d)
    if row is None:
        row = DomainPermission(domain=d, smtp_probe_allowed=body.smtp_probe_allowed, basis=body.basis.strip(), granted_by=user.id)
        session.add(row)
    else:
        row.smtp_probe_allowed, row.basis, row.granted_by = body.smtp_probe_allowed, body.basis.strip(), user.id
    await audit(session, user, "smtp.permission_set", "domain", d, {"allowed": body.smtp_probe_allowed, "basis": body.basis})
    await session.commit()
    await session.refresh(row)
    return PermissionOut(domain=row.domain, smtp_probe_allowed=row.smtp_probe_allowed, basis=row.basis, granted_by=row.granted_by, granted_at=row.granted_at)


@router.delete("/domain-permissions/{domain}", response_model=Ok)
async def delete_domain_permission(domain: str, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> Ok:
    d = _norm_domain(domain)
    row = await session.get(DomainPermission, d)
    if row is None:
        raise ApiError(404, "not_found", "No permission is recorded for that domain.")
    await audit(session, user, "smtp.permission_delete", "domain", d)
    await session.delete(row)
    await session.commit()
    return Ok(message="Permission removed.")


