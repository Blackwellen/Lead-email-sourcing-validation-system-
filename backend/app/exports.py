"""Streaming exports (CSV / XLSX / JSONL) with presets, CSV-injection protection and bounded memory.

Rows are read in keyset batches (never the whole result set), written straight to a file under
``settings.export_dir`` with a random name, and only *known* values are emitted (empty string otherwise).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import and_, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from . import jobqueue as jq
from .api.common import capped_count
from .api.filters import ContactFilterSpec, build_conditions
from .config import get_settings
from .models import Category, Contact, ContactEvidence, EmailAddress, EvidenceSource, Export, Organisation, OrganisationDomain, User
from .security import ApiError
from .settings_schema import load_section
from .suppression import not_suppressed_conds
from .system_runs import ensure_system_run

log = logging.getLogger("exports")

COLUMNS = [
    "Email Address", "Email Status", "Confidence Score", "Published or Inferred", "First Name", "Last Name", "Full Name", "Job Title", "Department",
    "Company Name", "Company Legal Name", "Company Website", "Company Domain", "Company Registration Number", "Business Category", "Business Subcategory",
    "Country", "Region", "City", "Postal Code", "Business Phone", "Source URL", "Discovery Source", "Date Discovered", "Last Validated", "MX Status",
    "Catch-All Status", "Role Account", "Marketing Eligibility", "Suppression Status",
]
AUDIT_EXTRA = ["Email ID", "Origin", "Syntax Status", "Recipient Evidence Status", "Needs Review", "Previously Bounced", "Free Provider", "Disposable",
               "Role Type", "Last Seen", "Organisation ID", "Confidence Breakdown"]
LISTMONK_COLUMNS = ["email", "name", "attributes"]

PRESETS: dict[str, dict[str, Any]] = {
    "full_intelligence": {"label": "Full intelligence", "description": "Every known field for all matching contacts.", "defaults": {}},
    "published_business_emails": {"label": "Published business emails", "description": "Published addresses at business domains, suppressed excluded.",
                                  "defaults": {"published_only": True, "free_provider": False, "exclude_suppressed": True}},
    "named_decision_makers": {"label": "Named decision makers", "description": "Named individuals with a recorded job title.",
                              "defaults": {"contact_type": "named", "has_job_title": True, "exclude_suppressed": True}},
    "role_company_emails": {"label": "Role / company emails", "description": "Role mailboxes (info@, sales@ ...).",
                            "defaults": {"contact_type": "role", "free_provider": False, "exclude_suppressed": True}},
    "one_per_company": {"label": "One per company", "description": "The best address for each business.", "defaults": {"uniqueness": "organisation", "exclude_suppressed": True}},
    "one_per_domain": {"label": "One per domain", "description": "The best address for each e-mail domain.", "defaults": {"uniqueness": "domain", "exclude_suppressed": True}},
    "listmonk_ready": {"label": "Listmonk-ready", "description": "Subscriber CSV (email,name,attributes); marketing-eligible and not suppressed only.",
                       "defaults": {"marketing_eligible_only": True, "exclude_suppressed": True}},
    "validation_audit": {"label": "Validation audit", "description": "All contacts with extra audit columns.", "defaults": {}},
}
FORMATS = ("csv", "xlsx", "jsonl")
BATCH = 2000
XLSX_MAX_ROWS = 1_048_000


class ExportFilters(ContactFilterSpec):
    published_only: bool = False
    uniqueness: Literal["email", "organisation", "domain"] = "email"
    exclude_suppressed: bool = False
    marketing_eligible_only: bool = False
    has_job_title: bool | None = None
    freshness_days: int | None = Field(None, ge=1, le=3650, description="Only contacts validated within this many days")


def apply_preset(preset: str, f: ExportFilters) -> ExportFilters:
    if preset not in PRESETS:
        raise ApiError(422, "invalid_preset", f"Unknown export preset '{preset}'. Choose one of: {', '.join(PRESETS)}.")
    data = f.model_dump()
    for k, v in PRESETS[preset]["defaults"].items():
        if k not in f.model_fields_set:
            data[k] = v
    return ExportFilters.model_validate(data)


def csv_safe(v: Any) -> Any:
    """OWASP CSV-injection guard: text cells that a spreadsheet could treat as a formula get a leading single quote."""
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _iso(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


E, O, C = EmailAddress, Organisation, Contact
Cat = aliased(Category)
PCat = aliased(Category)


def _dom_subq(col):
    d = aliased(OrganisationDomain)
    return (
        select(getattr(d, col)).where(d.organisation_id == O.id, d.parked.is_(False), d.verified_match_status.in_(("confirmed", "high")))
        .order_by((d.verified_match_status == "confirmed").desc(), d.confidence.desc(), d.id).limit(1).correlate(O).scalar_subquery()
    )


def _src_subq():
    return (
        select(EvidenceSource.source_url).select_from(ContactEvidence.__table__.join(EvidenceSource.__table__, EvidenceSource.id == ContactEvidence.evidence_id))
        .where(ContactEvidence.email_id == E.id, EvidenceSource.source_url.is_not(None)).order_by(ContactEvidence.observed_at, ContactEvidence.id).limit(1).correlate(E).scalar_subquery()
    )


async def filter_conditions(session: AsyncSession, f: ExportFilters) -> list:
    conds = await build_conditions(session, f)
    if f.published_only:
        conds.append(E.origin.in_(("published", "corroborated")))
    if f.exclude_suppressed:
        conds.extend(not_suppressed_conds(E))
    if f.marketing_eligible_only:
        conds.append(E.marketing_status == "eligible")
        conds.extend(not_suppressed_conds(E))
    if f.has_job_title:
        conds.append(and_(C.job_title.is_not(None), C.job_title != ""))
    if f.freshness_days:
        conds.append(E.last_validated_at >= func.now() - func.make_interval(0, 0, 0, f.freshness_days))
    return conds


def _from():
    return (
        E.__table__.join(O.__table__, O.id == E.organisation_id).outerjoin(C.__table__, C.id == E.contact_id)
        .outerjoin(Cat.__table__, Cat.id == O.category_id).outerjoin(PCat.__table__, PCat.id == Cat.parent_id)
    )


def _select_cols():
    return [
        E.id.label("e_id"), E.email_normalised, E.overall_reachability, E.confidence, E.origin, E.role_type, E.domain_mail_status, E.catch_all_status,
        E.marketing_status, E.suppressed_at, E.first_seen_at, E.last_validated_at, E.last_seen_at, E.syntax_status, E.recipient_evidence_status,
        E.needs_review, E.previously_bounced, E.is_free_provider, E.is_disposable, E.confidence_breakdown, E.organisation_id, E.registrable_domain,
        C.given_name, C.family_name, C.full_name, C.job_title, C.department,
        O.legal_name, O.trading_name, O.company_number, O.country_code, O.region, O.city, O.postal_code, O.phone, O.discovery_source, O.industry, O.subcategory,
        Cat.name.label("cat_name"), PCat.name.label("parent_name"),
        _dom_subq("domain").label("co_domain"), _dom_subq("website_url").label("co_website"), _src_subq().label("source_url"),
    ]


def _suppression_status(m) -> str:
    return "suppressed" if m["suppressed_at"] is not None or m["marketing_status"] in ("suppressed", "opted_out") else "not_suppressed"


def full_row(m, audit_cols: bool) -> list[Any]:
    cat, sub = m["cat_name"], m["subcategory"] or ""
    if cat and m["parent_name"]:
        cat, sub = m["parent_name"], m["cat_name"]
    elif not cat:
        cat = m["industry"] or ""
    row = [
        m["email_normalised"], m["overall_reachability"], round(m["confidence"], 1), "Published" if m["origin"] in ("published", "corroborated") else "Inferred",
        m["given_name"] or "", m["family_name"] or "", m["full_name"] or "", m["job_title"] or "", m["department"] or "",
        m["trading_name"] or m["legal_name"] or "", m["legal_name"] or "", m["co_website"] or "", m["co_domain"] or "", m["company_number"] or "",
        cat or "", sub or "", m["country_code"] or "", m["region"] or "", m["city"] or "", m["postal_code"] or "", m["phone"] or "",
        m["source_url"] or "", m["discovery_source"] or "", _iso(m["first_seen_at"]), _iso(m["last_validated_at"]), m["domain_mail_status"],
        m["catch_all_status"], "yes" if m["role_type"] else "no", m["marketing_status"], _suppression_status(m),
    ]
    if audit_cols:
        row += [m["e_id"], m["origin"], m["syntax_status"], m["recipient_evidence_status"], "yes" if m["needs_review"] else "no", "yes" if m["previously_bounced"] else "no",
                "yes" if m["is_free_provider"] else "no", "yes" if m["is_disposable"] else "no", m["role_type"] or "", _iso(m["last_seen_at"]), m["organisation_id"],
                json.dumps(m["confidence_breakdown"] or {}, sort_keys=True)]
    return row


def listmonk_row(m) -> list[Any]:
    full = m["full_name"] or " ".join(x for x in (m["given_name"], m["family_name"]) if x)
    attrs = {
        "company": m["trading_name"] or m["legal_name"], "domain": m["registrable_domain"], "category": m["parent_name"] or m["cat_name"] or m["industry"],
        "subcategory": m["cat_name"] if m["parent_name"] else m["subcategory"], "country": m["country_code"], "region": m["region"], "city": m["city"],
        "job_title": m["job_title"], "source": m["discovery_source"], "confidence": round(m["confidence"], 1), "published": m["origin"] in ("published", "corroborated"),
        "role_account": bool(m["role_type"]), "last_validated": _iso(m["last_validated_at"]) or None, "source_url": m["source_url"],
    }
    attrs = {k: v for k, v in attrs.items() if v not in (None, "")}
    return [m["email_normalised"], full or m["trading_name"] or m["legal_name"] or "", json.dumps(attrs, ensure_ascii=False, sort_keys=True)]


def header_for(preset: str) -> list[str]:
    if preset == "listmonk_ready":
        return LISTMONK_COLUMNS
    return COLUMNS + (AUDIT_EXTRA if preset == "validation_audit" else [])


async def _batches(session: AsyncSession, f: ExportFilters, limit: int) -> AsyncIterator[list]:
    conds = await filter_conditions(session, f)
    key = {"email": E.id, "organisation": E.organisation_id, "domain": E.registrable_domain}[f.uniqueness]
    last = None
    produced = 0
    while produced < limit:
        stmt = select(*_select_cols()).select_from(_from()).where(*conds)
        if last is not None:
            stmt = stmt.where(key > last)
        if f.uniqueness == "email":
            stmt = stmt.order_by(E.id)
        else:
            quality = case((E.origin.in_(("published", "corroborated")), 0), else_=1)
            stmt = stmt.distinct(key).order_by(key, quality, E.confidence.desc(), E.id)
        rows = (await session.execute(stmt.limit(min(BATCH, limit - produced)))).all()
        if not rows:
            return
        produced += len(rows)
        last = {"email": rows[-1]._mapping["e_id"], "organisation": rows[-1]._mapping["organisation_id"], "domain": rows[-1]._mapping["registrable_domain"]}[f.uniqueness]
        yield rows
        # release row objects before the next round trip
        del rows


class _Writer:
    def __init__(self, path: Path, fmt: str, header: list[str]):
        self.fmt, self.header, self.count = fmt, header, 0
        self._xl_rows = 0
        self._sheets = 0
        if fmt == "csv":
            self.fh = open(path, "w", newline="", encoding="utf-8")  # noqa: SIM115
            self.w = csv.writer(self.fh, quoting=csv.QUOTE_MINIMAL)
            self.w.writerow(header)
        elif fmt == "jsonl":
            self.fh = open(path, "w", encoding="utf-8")  # noqa: SIM115
        else:
            from openpyxl import Workbook

            self.path = path
            self.wb = Workbook(write_only=True)
            self._new_sheet()

    def _new_sheet(self):
        self.ws = self.wb.create_sheet(title="Export" if self._sheets == 0 else f"Export {self._sheets + 1}")
        self._sheets += 1
        self.ws.append([csv_safe(h) for h in self.header])
        self._xl_rows = 1

    def write(self, row: list[Any]) -> None:
        self.count += 1
        if self.fmt == "csv":
            self.w.writerow([csv_safe(v) for v in row])
        elif self.fmt == "jsonl":
            self.fh.write(json.dumps(dict(zip(self.header, row, strict=True)), ensure_ascii=False, default=str) + "\n")
        else:
            if self._xl_rows >= XLSX_MAX_ROWS:
                self._new_sheet()
            self.ws.append([csv_safe(v) for v in row])
            self._xl_rows += 1

    def close(self) -> None:
        if self.fmt == "xlsx":
            self.wb.save(self.path)
        else:
            self.fh.close()

    def abort(self) -> None:
        if self.fmt != "xlsx":
            try:
                self.fh.close()
            except Exception:
                pass


def export_dir() -> Path:
    p = Path(get_settings().export_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def resolve_export_path(file_name: str | None) -> Path | None:
    """Safe path of a stored export file (None unless it lives directly inside the export directory)."""
    if not file_name:
        return None
    base = export_dir()
    p = (base / Path(file_name).name).resolve()
    if p.parent != base or not p.is_file():
        return None
    return p


async def run_export(session: AsyncSession, export_id: int) -> Export:
    """Execute an export job: stream rows to a file, then record row_count/size/status."""
    ex = await session.get(Export, export_id)
    if ex is None:
        raise ValueError(f"export {export_id} not found")
    if ex.status == "done":
        return ex
    ex.status, ex.error = "running", None
    await session.commit()
    ex = await session.get(Export, export_id)
    max_rows = (await load_section(session, "exports")).max_rows
    fmt = ex.format
    path = export_dir() / f"{secrets.token_hex(16)}.{fmt}"
    tmp = path.with_suffix(path.suffix + ".part")
    writer = None
    try:
        f = ExportFilters.model_validate({k: v for k, v in (ex.filters or {}).items() if not k.startswith("_")})
        f = apply_preset(ex.preset, f)
        listmonk = ex.preset == "listmonk_ready"
        audit_cols = ex.preset == "validation_audit"
        writer = _Writer(tmp, fmt, header_for(ex.preset))
        async for rows in _batches(session, f, max_rows):
            for r in rows:
                m = r._mapping
                writer.write(listmonk_row(m) if listmonk else full_row(m, audit_cols))
            if writer.count % (BATCH * 5) == 0:
                await session.rollback()  # keep the read transaction short; nothing to keep
        writer.close()
        os.replace(tmp, path)
        ex = await session.get(Export, export_id)
        ex.status, ex.row_count, ex.file_path, ex.file_size = "done", writer.count, path.name, path.stat().st_size
        if writer.count >= max_rows:
            ex.filters = {**(ex.filters or {}), "_truncated_at": max_rows}
        await session.commit()
        return ex
    except Exception as e:
        log.exception("export %s failed", export_id)
        if writer:
            writer.abort()
        for p in (tmp, path):
            p.unlink(missing_ok=True)
        await session.rollback()
        ex = await session.get(Export, export_id)
        ex.status, ex.error = "failed", f"{type(e).__name__}: {str(e)[:300]}"
        await session.commit()
        return ex


async def estimate_rows(session: AsyncSession, f: ExportFilters, cap: int = 100_000) -> tuple[int | None, bool]:
    conds = await filter_conditions(session, f)
    stmt = select(literal_column("1")).select_from(_from()).where(*conds)
    if f.uniqueness == "organisation":
        stmt = select(E.organisation_id).select_from(_from()).where(*conds).distinct()
    elif f.uniqueness == "domain":
        stmt = select(E.registrable_domain).select_from(_from()).where(*conds).distinct()
    return await capped_count(session, stmt, cap=cap, timeout_ms=1500)


async def create_export(
    session: AsyncSession, *, user: User, preset: str, fmt: str, filters: ExportFilters, run_id: int | None = None, run_inline: bool | None = None
) -> Export:
    if fmt not in FORMATS:
        raise ApiError(422, "invalid_format", f"Format must be one of: {', '.join(FORMATS)}.")
    f = apply_preset(preset, filters)
    if preset == "listmonk_ready":
        fmt = "csv"
    ex = Export(created_by=user.id, preset=preset, format=fmt, filters=filters.model_dump(mode="json", exclude_defaults=True), status="queued", run_id=run_id)
    session.add(ex)
    await session.flush()
    threshold = (await load_section(session, "exports")).inline_threshold
    inline = run_inline
    if inline is None:
        est, capped = await estimate_rows(session, f, cap=threshold + 1)
        inline = est is not None and not capped and est < threshold
    if inline:
        await session.commit()
        return await run_export(session, ex.id)
    sys_run = await ensure_system_run(session, "system-export")
    await jq.enqueue(session, run_id=sys_run, stage="export", dedupe_key=f"export:{ex.id}", payload={"export_id": ex.id}, priority=30)
    return ex
